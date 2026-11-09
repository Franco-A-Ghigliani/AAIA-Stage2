"""Repository navigation and guarded editing within a disposable checkout."""

from __future__ import annotations

import ast
import os
import re
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath

from .checkout import git_output
from .errors import Denied, ToolError
from .text_edits import align_replacement, atomic_write, validate_source

LIMIT = 12_000  # chars per read, bytes per edited file
MAX_LIST = 200
MAX_MATCHES = 50
MAX_REFERENCES = 30


class WorkingTree:
    """Expose scoped navigation and replacement operations on one disposable checkout."""

    def __init__(self, root: Path | str, writable: Sequence[str] = (), limit: int = LIMIT):
        """Create the tools.

        Args:
            root: Repository copy; every path argument is relative to it.
            writable: Relative files or directories that replace_text may change.
                Empty means nothing is writable.
            limit: Maximum characters returned by a read and bytes of an edited file.
        """
        self.root = Path(root).resolve()
        self.writable = tuple(p.strip("/") for p in writable)
        self.limit = limit

    def allows_write(self, relative: str) -> bool:
        """Match a relative path against the configured writable files and directory prefixes."""
        for prefix in self.writable:
            if relative == prefix or relative.startswith(prefix + "/"):
                return True
        return False

    def _resolve_path(self, path: str, write: bool = False) -> Path:
        """Resolve a visible relative path, rejecting escapes and unauthorized writes."""
        pure = PurePosixPath(path)
        if not path or pure.is_absolute() or Path(path).is_absolute():
            raise Denied("path must be relative to the repository")
        if any(part == ".." or part.startswith(".") for part in pure.parts):
            raise Denied("'..' and hidden paths are not allowed")
        real = (self.root / pure).resolve()  # follows symlinks, so escapes are caught
        try:
            relative = real.relative_to(self.root).as_posix()
        except ValueError:
            raise Denied("path resolves outside the repository") from None
        if write and not self.allows_write(relative):
            raise Denied(f"{relative} is outside the writable area {list(self.writable)}")
        return real

    def _visible_paths(self) -> Iterator[str]:
        """Walk visible entries in sorted order without traversing directory symlinks."""
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for filename in sorted(filenames):
                if not filename.startswith("."):
                    yield (Path(dirpath) / filename).relative_to(self.root).as_posix()

    def enumerate_files(self) -> list[str]:
        """Collect the first visible paths, adding a marker when the listing is capped."""
        files: list[str] = []
        for relative in self._visible_paths():
            if len(files) == MAX_LIST:
                files.append(f"[shortened: showing the first {MAX_LIST} files]")
                break
            files.append(relative)
        return files

    def read_text(self, path: str) -> str:
        """Read bounded UTF-8 content and report missing or non-text files as tool errors."""
        real = self._resolve_path(path)
        if not real.is_file():
            raise ToolError(f"not a file: {path}")
        try:
            with real.open(encoding="utf-8") as handle:
                text = handle.read(self.limit + 1)
        except UnicodeDecodeError:
            raise ToolError(f"not UTF-8 text: {path}") from None
        if len(text) > self.limit:
            return text[: self.limit] + f"\n[shortened: showing the first {self.limit} characters]"
        return text

    def search_text(self, query: str) -> list[dict]:
        """Collect bounded literal matches with their paths, line numbers, and excerpts."""
        if not query:
            raise ToolError("query must not be empty")
        matches: list[dict] = []
        for count, relative in enumerate(self._visible_paths()):
            if count == MAX_LIST:
                matches.append({"shortened": f"searched only the first {MAX_LIST} files"})
                break
            if len(matches) == MAX_MATCHES:
                break
            try:
                with self._resolve_path(relative).open("rb") as handle:
                    data = handle.read(1_000_000)
                if b"\0" in data:
                    continue  # binary
                lines = data.decode("utf-8").splitlines()
            except (Denied, OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(lines, 1):
                if query in line:
                    matches.append(
                        {"path": relative, "line": number, "excerpt": line.strip()[:200]}
                    )
                    if len(matches) == MAX_MATCHES:
                        break
        return matches

    def replace_text(self, path: str, old: str, new: str) -> str:
        """Replace one matching block, falling back to indentation alignment when necessary.

        Validate size and Python syntax before committing through an atomic replacement."""
        real = self._resolve_path(path, write=True)
        if not real.is_file():
            raise ToolError(f"not a file: {path}")
        if real.stat().st_size > self.limit:
            raise ToolError(f"{path} is larger than {self.limit} bytes")
        if not old:
            raise ToolError("old text must not be empty")
        text = real.read_text(encoding="utf-8")
        count = text.count(old)
        candidates = [(text.replace(old, new), "")] if count == 1 else []
        if count <= 1 and (shifted := align_replacement(text, old, new)) is not None:
            candidates.append(
                (shifted, " (old matched once ignoring indentation; new was re-indented)")
            )
        if not candidates:
            raise ToolError(f"old text occurs {count} times; it must occur exactly once")
        failure = None
        for updated, note in candidates:
            try:
                if len(updated.encode("utf-8")) > self.limit:
                    raise ToolError(f"edited file would exceed {self.limit} bytes")
                validate_source(path, updated)
            except ToolError as exc:
                failure = failure or exc
                continue
            atomic_write(real, updated)
            return f"edited {path}{note}"
        raise failure

    def _parsed_sources(self) -> Iterator[tuple[str, ast.Module, str]]:
        """Yield readable Python sources and their syntax trees within the traversal cap."""
        for count, relative in enumerate(self._visible_paths()):
            if count == MAX_LIST:
                return
            if not relative.endswith(".py"):
                continue
            try:
                source = self._resolve_path(relative).read_text(encoding="utf-8")
                yield relative, ast.parse(source), source
            except (Denied, OSError, SyntaxError, UnicodeDecodeError, ValueError):
                continue

    def symbol_outline(self) -> list[str]:
        """Summarize top-level definitions while retaining paths for other visible files."""
        python: dict[str, str] = {}
        for relative, tree, _source in self._parsed_sources():
            parts = []
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    methods = [
                        item.name
                        for item in node.body
                        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                    ]
                    parts.append(f"class {node.name}({', '.join(methods)})")
                elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    parts.append(f"def {node.name}")
            python[relative] = f"{relative}: {'; '.join(parts)}" if parts else relative
        return [python.get(path, path) for path in self.enumerate_files()]

    def locate_symbol(self, name: str) -> dict:
        """Locate Python definitions and bounded textual references to an identifier."""
        if not name.isidentifier():
            raise ToolError("name must be a Python identifier")
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        definitions, references = [], []
        for relative, tree, source in self._parsed_sources():
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                    and node.name == name
                ):
                    kind = "class" if isinstance(node, ast.ClassDef) else "function"
                    definitions.append({"path": relative, "line": node.lineno, "kind": kind})
            for number, line in enumerate(source.splitlines(), 1):
                if pattern.search(line) and len(references) < MAX_REFERENCES:
                    references.append(
                        {"path": relative, "line": number, "excerpt": line.strip()[:200]}
                    )
        return {"definitions": definitions, "references": references}

    @staticmethod
    def _import_names(relative: str) -> list[str]:
        """Derive importable module names with and without a leading src directory."""
        parts = relative.removesuffix(".py").split("/")
        if parts[-1] == "__init__":
            parts.pop()
        names = [".".join(parts)]
        if parts[:1] == ["src"]:
            names.append(".".join(parts[1:]))
        return names

    def _referenced_modules(
        self, relative: str, tree: ast.Module, modules: dict[str, str]
    ) -> set[str]:
        """Resolve absolute and relative imports to paths in the repository."""
        package = self._import_names(relative)[-1].split(".")
        if not relative.endswith("__init__.py"):
            package = package[:-1]
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(modules.get(alias.name, "") for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                prefix = package[: len(package) - node.level + 1] if node.level else []
                base = ".".join([*prefix, *(node.module or "").split(".")]).strip(".")
                for alias in node.names:
                    found.add(modules.get(f"{base}.{alias.name}") or modules.get(base, ""))
        return found - {"", relative}

    def import_relationships(self, path: str) -> dict:
        """Report the requested Python module’s imports and reverse dependencies."""
        wanted = self._resolve_path(path).relative_to(self.root).as_posix()
        trees = {relative: tree for relative, tree, _source in self._parsed_sources()}
        if wanted not in trees:
            raise ToolError(f"not a parsable Python file: {path}")
        modules = {name: relative for relative in trees for name in self._import_names(relative)}
        graph = {
            relative: self._referenced_modules(relative, tree, modules)
            for relative, tree in trees.items()
        }
        return {
            "imports": sorted(graph[wanted]),
            "imported_by": sorted(file for file, used in graph.items() if wanted in used),
        }

    def describe_changes(self) -> dict:
        """Collect HEAD, modified paths, and the current diff summary."""
        status = git_output(self.root, "status", "--porcelain", "--untracked-files=all")
        return {
            "head": git_output(self.root, "rev-parse", "HEAD").strip(),
            "changed_files": sorted(line[3:] for line in status.splitlines() if line.strip()),
            "diff_stat": git_output(self.root, "diff", "--stat", "HEAD").strip(),
        }
