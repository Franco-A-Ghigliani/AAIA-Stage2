"""Disposable source snapshots and fail-closed Docker command execution."""

import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


class SandboxError(Exception):
    pass


def snapshot_repository(source, destination):
    """Copy visible tracked files, never Git metadata, links or host extras.

    The target must be a secret-free source repository. Common credential files
    are excluded as defense in depth; arbitrary secrets embedded in source cannot
    be identified automatically. Untracked files are deliberately not copied.
    """
    source = Path(source).resolve(strict=True)
    result = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
         "-c", "core.hooksPath=/dev/null", "-c", f"safe.directory={source}",
         "-C", str(source),
         "ls-files", "--cached", "-z"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=True,
    )
    for name in os.fsdecode(result.stdout).split("\0"):
        if not name:
            continue
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise SandboxError("invalid tracked path")
        if any(part.startswith(".") for part in relative.parts):
            continue
        if (relative.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
                or any(word in relative.name.lower()
                       for word in ("credential", "secret", "id_rsa", "id_ed25519"))):
            continue
        origin = source / relative
        # Reject every link/reparse point, including parent junctions on Windows.
        for entry in [origin, *origin.parents]:
            if entry == source:
                break
            if entry.is_symlink() or entry.is_junction():
                raise SandboxError(f"tracked links are not allowed: {name}")
        origin.resolve(strict=True).relative_to(source)
        if not origin.is_file():
            raise SandboxError(f"tracked entry is not a regular file: {name}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, target)
        target.chmod(0o777 if origin.stat().st_mode & 0o111 else 0o666)


class DockerSandbox:
    """One disposable, networkless container per command; no host fallback."""

    def __init__(self, root, image="coding-harness-sandbox:local"):
        self.root = Path(root)
        self.image = image
        self.broken = False

    def _control(self, *args):
        return subprocess.run(
            ["docker", *args], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
            check=True,
        )

    def cleanup_session(self, session_id):
        """Remove any container left behind by a process interruption."""
        if not session_id:
            return
        label = f"coding-harness.session={session_id}"
        result = self._control("ps", "--all", "--quiet", "--filter", f"label={label}")
        names = result.stdout.decode("utf-8", errors="replace").split()
        if names:
            self._control("rm", "--force", *names)

    def run(self, command, timeout, limit, *, session_id=None, readonly_mounts=None,
            readonly_workspace=False):
        if self.broken:
            raise SandboxError("sandbox cleanup failed; further actions disabled")
        name = "coding-harness-" + uuid.uuid4().hex
        if session_id:
            name = "coding-harness-" + session_id[:12] + "-" + uuid.uuid4().hex[:12]
        child = None
        reason = None
        output = b""
        # A file avoids unbounded buffering and pipe/selector portability issues.
        with tempfile.TemporaryFile() as captured:
            try:
                workspace_mount = f"type=bind,source={str(self.root).replace(chr(92), '/')},target=/workspace"
                if readonly_workspace:
                    workspace_mount += ",readonly"
                create_args = [
                    "create", "--name", name, "--pull=never",
                    "--network=none", "--read-only", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges", "--pids-limit=128",
                    "--memory=512m", "--cpus=1", "--user=65534:65534",
                    "--mount", workspace_mount,
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                    "--workdir=/workspace", "--env=HOME=/tmp",
                    "--env=PATH=/usr/local/bin:/usr/bin:/bin",
                    "--env=LANG=C.UTF-8", "--env=PYTHONDONTWRITEBYTECODE=1",
                    "--entrypoint=/usr/bin/timeout", self.image,
                    # PID 1 has its own deadline even if the harness is killed.
                    "--signal=KILL", str(timeout),
                    "/bin/bash", "--noprofile", "--norc", "-c", command,
                ]
                if session_id:
                    create_args[3:3] = ["--label", f"coding-harness.session={session_id}"]
                mount_args = []
                for index, path in enumerate(readonly_mounts or []):
                    mount_args.extend([
                        "--mount",
                        f"type=bind,source={str(Path(path).resolve()).replace(chr(92), '/')},target=/protected/{index},readonly",
                    ])
                insertion = create_args.index("--workdir=/workspace")
                create_args[insertion:insertion] = mount_args
                self._control(*create_args)
                child = subprocess.Popen(
                    ["docker", "start", "--attach", name],
                    stdin=subprocess.DEVNULL, stdout=captured,
                    stderr=subprocess.STDOUT,
                )
                deadline = time.monotonic() + timeout
                while True:
                    if os.fstat(captured.fileno()).st_size > limit:
                        reason = f"output exceeded {limit} bytes"
                        break
                    if time.monotonic() >= deadline:
                        reason = f"timed out after {timeout}s"
                        break
                    if child.poll() is not None:
                        break
                    time.sleep(0.02)
                captured.seek(0)
                output = captured.read(limit)
                if reason is None:
                    inspected = self._control(
                        "inspect", "--format={{.State.ExitCode}}", name)
                    exit_code = int(inspected.stdout.strip())
                    if exit_code == 137:
                        reason = "container killed (deadline or resource limit)"
                else:
                    exit_code = None
            finally:
                # Remove on EVERY exit, including normal completion, errors and
                # cancellation. Docker destroys detached/session-escaped children.
                try:
                    self._control("rm", "--force", name)
                except (OSError, subprocess.SubprocessError):
                    self.broken = True
                    raise SandboxError(
                        f"could not confirm container destruction: {name}; "
                        "further actions disabled") from None
                finally:
                    if child is not None:
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=5)
        result = {"exit_code": exit_code,
                  "output": output.decode("utf-8", errors="replace")}
        if reason:
            result["stopped"] = reason
        return result
