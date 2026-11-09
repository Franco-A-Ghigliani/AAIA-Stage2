"""Run named checks inside restricted Docker containers."""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from .errors import ToolError
from .processes import LIMIT, capture_process

DOCKER_ERROR = 125


class ContainerChecks:
    """Translate allowlisted check names into isolated Docker invocations."""

    def __init__(
        self,
        workspace: Path | str,
        image: str,
        commands: Mapping[str, Sequence[str]],
        mounts: Mapping[str, Path | str] | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 60,
        limit: int = LIMIT,
        docker: str = "docker",
    ):
        """Create the environment.

        Args:
            workspace: Disposable repository copy, mounted read-only at /work.
            image: Container image with the target's import_relationships preinstalled.
            commands: Check name -> argv run inside the container.
            mounts: Container path -> host path, mounted read-only.
            env: Environment variables set inside the container.
            timeout: Seconds allowed per check.
            limit: Output bytes kept per check.
            docker: Container CLI to call.
        """
        self.workspace = Path(workspace).resolve()
        self.image = image
        self.commands = {name: list(argv) for name, argv in commands.items()}
        self.mounts = {target: Path(source).resolve() for target, source in (mounts or {}).items()}
        self.env = dict(env or {})
        self.timeout = timeout
        self.limit = limit
        self.docker = docker
        self._container: str | None = None

    def container_arguments(self, name: str, container: str) -> list[str]:
        """Construct the ordered Docker arguments, resource limits, and read-only mounts."""
        argv = [self.docker, "run", "--rm", "--pull", "never", "--name", container]
        argv += ["--network", "none", "--read-only", "--tmpfs", "/tmp"]
        argv += ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
        argv += ["--pids-limit", "256", "--memory", "1g", "--cpus", "2"]
        argv += ["--user", "65534:65534", "-e", "HOME=/tmp"]
        for key, value in self.env.items():
            argv += ["-e", f"{key}={value}"]
        argv += ["-v", f"{self.workspace}:/work:ro", "-w", "/work"]
        for target, source in self.mounts.items():
            argv += ["-v", f"{source}:{target}:ro"]
        return argv + [self.image] + self.commands[name]

    def check(self, name: str) -> dict:
        """Run one named check and retain its output, exit code, and availability status."""
        if name not in self.commands:
            raise ToolError(f"unknown check: {name!r}; configured checks: {sorted(self.commands)}")
        self._container = f"harness-{uuid.uuid4().hex[:12]}"
        try:
            result = capture_process(
                self.container_arguments(name, self._container),
                self.timeout,
                self.limit,
                on_stop=self.stop,
            )
        except FileNotFoundError:
            result = {
                "exit_code": None,
                "output": f"{self.docker} is not installed",
                "shortened": False,
                "stopped": None,
            }
        finally:
            self._container = None
        status = _check_status(result)
        stopped = result.pop("stopped")
        if stopped:
            result["output"] += f"\n[stopped: {stopped}]"
        return {"check": name, "status": status, **result}

    def stop(self) -> None:
        """Terminate the currently tracked container, if one exists."""
        if self._container:
            subprocess.run(
                [self.docker, "kill", self._container],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )


def _check_status(result: dict) -> str:
    """Distinguish check failures from failures to start the container."""
    if result["stopped"]:
        return "stopped"
    if result["exit_code"] in (None, DOCKER_ERROR):
        return "unavailable"
    return "passed" if result["exit_code"] == 0 else "failed"
