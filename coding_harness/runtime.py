import ast
import fnmatch
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

if __package__:
    from . import tool_repository
    from .sandbox import DockerSandbox, SandboxError, snapshot_repository
    from .navigation import discover_dependencies, find_python_symbols, git_state, repository_map
else:
    import tool_repository
    from sandbox import DockerSandbox, SandboxError, snapshot_repository
    from navigation import discover_dependencies, find_python_symbols, git_state, repository_map

LIMIT = 12_000
MAX_LIST = 200
MAX_MATCHES = 30
BASH_TIMEOUT = 20
FETCH_TIMEOUT = 10
MUTATING = {"write_file", "edit_file", "delete_file", "bash"}

class Denied(Exception):
    """Policy refused the request."""

class ToolError(Exception):
    """The request was allowed but could not be completed."""

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ToolError("redirects are not allowed")

def _open_url(url):
    """Open an HTTPS URL without following redirects (patched in tests)."""
    opener = urllib.request.build_opener(_NoRedirect)
    return opener.open(urllib.request.Request(url, method="GET"), timeout=FETCH_TIMEOUT)


def _check_python(path, source):
    """Validate Python before committing a file change."""
    if Path(path).suffix != ".py":
        return
    try:
        ast.parse(source, filename=path)
    except (SyntaxError, ValueError) as error:
        raise ToolError(f"invalid Python in {path}: {error}") from None


def _bounded_result(status, output):
    """Keep every model-facing tool result inside the shared output limit."""
    result = {"status": status, "output": output}
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    encoded = serialized.encode("utf-8")
    if len(encoded) <= LIMIT:
        return result
    marker = "\n[truncated: tool output exceeded 12000 characters]"
    low, high = 0, len(encoded)
    preview = marker
    while low <= high:
        middle = (low + high) // 2
        candidate = encoded[:middle].decode("utf-8", errors="ignore") + marker
        response = {"status": status, "output": {"truncated": True, "preview": candidate}}
        if len(json.dumps(response, ensure_ascii=False).encode("utf-8")) <= LIMIT:
            preview = candidate
            low = middle + 1
        else:
            high = middle - 1
    return {"status": status, "output": {"truncated": True, "preview": preview}}


def _reindented_edit(source, before, after):
    """Recover a unique whole-line edit whose indentation was lost."""
    lines = source.splitlines(keepends=True)
    target = before.splitlines()
    if not target or not any(line.strip() for line in target):
        return None
    locations = [
        start for start in range(len(lines) - len(target) + 1)
        if [line.lstrip().rstrip("\r\n") for line in lines[start:start + len(target)]]
        == [line.lstrip() for line in target]
    ]
    if len(locations) != 1:
        return None
    start = locations[0]
    original = lines[start:start + len(target)]
    anchor = next(index for index, line in enumerate(target) if line.strip())
    actual_indent = original[anchor][:len(original[anchor]) - len(original[anchor].lstrip())]
    given_indent = target[anchor][:len(target[anchor]) - len(target[anchor].lstrip())]
    replacement = []
    for line in after.splitlines(keepends=True):
        if line.strip():
            if given_indent and not line.startswith(given_indent):
                return None
            line = actual_indent + line[len(given_indent):]
        replacement.append(line)
    # A block replacement should not join its last line to the next statement.
    if replacement and original[-1].endswith("\n") and not replacement[-1].endswith("\n"):
        replacement[-1] += "\r\n" if original[-1].endswith("\r\n") else "\n"
    return "".join(lines[:start] + replacement + lines[start + len(target):])


class Runtime:
    """Validate and execute tools."""

    def __init__(self, root, *, approve=None, enabled=None,
                 image="coding-harness-sandbox:local", workspace=None,
                 initialize=True, allowed_changes=None, session_id=None):
        """Create a runtime.

        Args:
            root: Secret-free target Git repository; only tracked source is copied.
        """
        self.source_root = Path(root).resolve(strict=True)
        self._temporary = None
        self.session_id = session_id
        self.allowed_changes = list(["**"] if allowed_changes is None else allowed_changes)
        if workspace is None:
            self._temporary = tempfile.TemporaryDirectory(prefix="coding-harness-")
            self.root = Path(self._temporary.name) / "workspace"
            self.root.mkdir()
        else:
            self.root = Path(workspace).resolve()
            if self.root.is_symlink() or self.root.is_junction():
                raise SandboxError("workspace cannot be a link")
            if initialize:
                self.root.mkdir(parents=True, exist_ok=True)
                if any(self.root.iterdir()):
                    raise SandboxError("new workspace must be empty")
            elif not self.root.is_dir():
                raise SandboxError("saved workspace is missing")
        try:
            if initialize:
                snapshot_repository(self.source_root, self.root)
        except BaseException:
            if self._temporary is not None:
                self._temporary.cleanup()
            raise
        # The container runs as an unprivileged user on Linux.
        self.root.chmod(0o777)
        for entry in self.root.rglob("*"):
            if entry.is_symlink() or entry.is_junction():
                continue
            entry.chmod(0o777 if entry.is_dir() or entry.stat().st_mode & 0o111 else 0o666)
        self.sandbox = DockerSandbox(self.root, image=image)
        self.approve = approve
        self.bash_timeout = BASH_TIMEOUT
        self.tools = [tool for tool in tool_repository.describe_tools()
                      if enabled is None or tool["name"] in enabled]

    def close(self):
        if self.sandbox.broken:
            if self._temporary is not None:
                self._temporary._finalizer.detach()
            raise SandboxError("cleanup not confirmed; retaining disposable workspace")
        if self._temporary is not None:
            self._temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    #POLICIES
    def tool_allowed(self, name):
        """True when the tool is enabled."""
        return any(tool["name"] == name for tool in self.tools)

    def _change_allowed(self, path):
        normalized = path.replace("\\", "/")
        for pattern in self.allowed_changes:
            candidate = pattern.replace("\\", "/").strip("/")
            if candidate in ("", "**", "*") or fnmatch.fnmatchcase(normalized, candidate):
                return True
            if not any(char in candidate for char in "*?[") and normalized.startswith(candidate + "/"):
                return True
        return False

    def _repo_map(self):
        return repository_map(self.root)

    def _find_symbols(self, query):
        if not query.strip():
            raise ToolError("query must not be empty")
        return find_python_symbols(self.root, query)

    def _dependencies(self):
        return discover_dependencies(self.root)

    def _git_status(self):
        return git_state(self.source_root)

    def run_check(self, command, readonly_mounts=None):
        """Run one saved user check in the contained environment."""
        if not isinstance(command, str) or not command.strip() or len(command) > LIMIT:
            raise ToolError(f"check command must be 1 to {LIMIT} characters")
        mounts = [Path(path).resolve(strict=True) for path in (readonly_mounts or [])]
        for path in mounts:
            if path.is_symlink() or path.is_junction():
                raise ToolError("protected check paths must exist and cannot be links")
            try:
                path.relative_to(self.root)
            except ValueError:
                pass
            else:
                raise Denied("protected checks must be outside the agent workspace")
        return self.sandbox.run(command, BASH_TIMEOUT, LIMIT,
                                session_id=self.session_id,
                                readonly_mounts=mounts,
                                readonly_workspace=True)

    def execute(self, action):
        """Execute the given action."""
        try:
            if self.sandbox.broken:
                raise Denied("sandbox cleanup failed; further actions disabled")
            if not isinstance(action, dict):
                raise ToolError('action must be {"tool": str, "args": object}')

            if set(action) != {"tool", "args"}:
                raise ToolError('action must be {"tool": str, "args": object}')

            name = action["tool"]
            args = action["args"]

            if not isinstance(name, str):
                raise ToolError("tool name must be a string")

            if not self.tool_allowed(name):
                raise ToolError(f"unknown tool: {name!r}")

            expected_args = tool_repository.get_tool(name)["args"]
            argument_error = f"{name} takes exactly these arguments: {expected_args}"

            if not isinstance(args, dict):
                raise ToolError(argument_error)

            if set(args) != set(expected_args):
                raise ToolError(argument_error)

            for key, value in args.items():
                error_message = (
                    f"argument {key!r} must be a string "
                    f"of at most {LIMIT} chars"
                )

                if not isinstance(value, str) or len(value) > LIMIT:
                    raise ToolError(error_message)

            if name in {"write_file", "edit_file", "delete_file"}:
                if not self._change_allowed(args.get("path", "")):
                    raise Denied("path is outside this task's allowed changes")
            if name == "bash" and self.allowed_changes != ["**"]:
                raise Denied("bash is disabled when the task has a restricted path scope")

            handler = getattr(self, "_" + name)
            result = handler(**args)
            return _bounded_result("ok", result)

        except Denied as error:
            return _bounded_result("denied", str(error))

        except (ToolError, SandboxError, subprocess.SubprocessError) as error:
            return _bounded_result("error", error.args[0] if error.args else str(error))

        except (OSError, UnicodeError) as error:
            return _bounded_result("error", f"{error.__class__.__name__}: {error}")

    def _resolve_path(self, path):
        """Map a relative path to a real path inside root, or raise Denied."""
        pure_path = PurePosixPath(path)

        if (not path or "\\" in path or ":" in path or "\0" in path
                or pure_path.is_absolute() or Path(path).is_absolute()):
            raise Denied("path must be relative to root using forward slashes")
        if any(part == ".." or part.startswith(".") for part in pure_path.parts):
            raise Denied("'..' and hidden directories are not allowed")
        real_path = (self.root / pure_path).resolve()
        for entry in [(self.root / pure_path), *(self.root / pure_path).parents]:
            if entry == self.root:
                break
            if entry.is_symlink() or entry.is_junction():
                raise Denied("links are not allowed")
        try:
            real_path.relative_to(self.root).as_posix()
        except ValueError:
            raise Denied("path is outside the workspace") from None

        return real_path

    def _walk_workspace(self):
        """Return relative paths of visible files, without following links."""
        for path, dir_names, file_names in os.walk(self.root, followlinks=False):
            visible_dirs = []
            for dirname in dir_names:
                directory = Path(path) / dirname
                if (not dirname.startswith(".") and not directory.is_symlink()
                        and not directory.is_junction()):
                    visible_dirs.append(dirname)
            dir_names[:] = sorted(visible_dirs)

            for filename in sorted(file_names):
                if filename.startswith(".") or (Path(path) / filename).is_symlink():
                    continue
                file_path = Path(path) / filename
                yield file_path.relative_to(self.root).as_posix()

    #TOOL HANDLERS
    def _list_files(self):
        paths = []
        for index, path in enumerate(self._walk_workspace()):
            if index < MAX_LIST:
                paths.append(path)
                continue
            paths.append(f"[truncated at {MAX_LIST} files]")
            break
        return paths

    def _read_file(self, path):
        location = self._resolve_path(path)
        if not location.is_file():
            raise ToolError(f"not a file: {path}")
        try:
            with location.open("r", encoding="utf-8") as stream:
                contents = stream.read(LIMIT + 1)
        except UnicodeDecodeError:
            raise ToolError(f"not UTF-8 text: {path}") from None
        if len(contents) <= LIMIT:
            return contents
        return f"{contents[:LIMIT]}\n[truncated: showing first {LIMIT} characters]"

    def _search_files(self, query):
        if query == "":
            raise ToolError("query must not be empty")
        found = []
        for index, path in enumerate(self._walk_workspace()):
            if index >= MAX_LIST or len(found) >= MAX_MATCHES:
                break
            try:
                location = self._resolve_path(path)
                if not location.is_file():
                    continue
                with location.open("rb") as stream:
                    contents = stream.read(1_000_000)
                if b"\x00" in contents:
                    continue
                lines = contents.decode("utf-8").splitlines()
            except (Denied, OSError, UnicodeDecodeError):
                continue
            for index, line in enumerate(lines, start=1):
                if query not in line:
                    continue
                found.append(dict(path=path, line=index, excerpt=line.strip()[:200]))
                if len(found) >= MAX_MATCHES:
                    break
        return found

    def _write_file(self, path, content):
        destination = self._resolve_path(path)
        self._validate_change(path, content, f"content exceeds {LIMIT} bytes")
        destination.parent.mkdir(parents=True, exist_ok=True)
        for directory in destination.parents:
            directory.chmod(0o777)
            if directory == self.root:
                break
        try:
            with destination.open(mode="x", encoding="utf-8") as stream:
                stream.write(content)
        except FileExistsError:
            raise ToolError(f"{path} already exists; use edit_file") from None
        destination.chmod(0o666)
        return f"created {path}"

    def _edit_file(self, path, old, new):
        destination = self._resolve_path(path)
        if not destination.is_file():
            raise ToolError(f"not a file: {path}")
        if destination.stat().st_size > LIMIT:
            raise ToolError(f"{path} is larger than {LIMIT} bytes")
        if old == "":
            raise ToolError("old text must not be empty")
        contents = destination.read_text(encoding="utf-8")
        occurrences = contents.count(old)
        options = []
        if occurrences == 1:
            options.append((contents.replace(old, new, 1), ""))
        if occurrences <= 1:
            recovered = _reindented_edit(contents, old, new)
            if recovered is not None:
                options.append((recovered, " (old matched once ignoring indentation; new was re-indented)"))
        if not options:
            raise ToolError(f"old text occurs {occurrences} times; it must occur exactly once")
        first_error = None
        for contents, suffix in options:
            try:
                self._validate_change(path, contents, f"edited file would exceed {LIMIT} bytes")
            except ToolError as error:
                if first_error is None:
                    first_error = error
            else:
                destination.write_text(contents, encoding="utf-8")
                return f"edited {path}{suffix}"
        raise first_error

    def _delete_file(self, path):
        destination = self._resolve_path(path)
        if not destination.is_file():
            raise ToolError(f"not a file: {path}")
        destination.unlink()
        return f"deleted {path}"

    @staticmethod
    def _validate_change(path, contents, size_error):
        if len(contents.encode("utf-8")) > LIMIT:
            raise ToolError(size_error)
        _check_python(path, contents)

    def _fetch_url(self, url):
        try:
            address = urlsplit(url)
            port = address.port
        except ValueError:
            raise Denied("invalid URL or port") from None
        permitted = (
            address.scheme == "https"
            and address.hostname == "docs.python.org"
            and address.username is None and address.password is None
            and port in (None, 443)
            and "?" not in url and "#" not in url
        )
        if not permitted:
            raise Denied("only https://docs.python.org URLs without credentials, "
                         "query, or fragment are allowed")
        else:
            origin = "live docs.python.org"
            try:
                with _open_url(url) as response:
                    mime_type = response.headers.get_content_type()
                    if mime_type != "application/xhtml+xml" and not mime_type.startswith("text/"):
                        raise ToolError(f"unsupported content type: {mime_type}")
                    contents = response.read(LIMIT + 1)
            except urllib.error.HTTPError as error:
                raise ToolError(f"HTTP {error.code} from {url}") from None
            except (urllib.error.URLError, TimeoutError) as error:
                raise ToolError(f"network error: {error}") from None
        return dict(source_url=url, source=origin, truncated=len(contents) > LIMIT,
                    content=contents[:LIMIT].decode("utf-8", errors="replace"))

    def _bash(self, command):
        if self.approve is None or self.approve(command, str(self.root)) is not True:
            raise Denied("bash requires explicit command approval")
        if self.session_id:
            result = self.sandbox.run(command, self.bash_timeout, LIMIT,
                                      session_id=self.session_id)
        else:
            result = self.sandbox.run(command, self.bash_timeout, LIMIT)
        if "stopped" in result:
            raise ToolError(result)
        return result
