"""Bounded subprocess capture with timeout and process-tree cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence

LIMIT = 12_000


def _child_environment() -> dict[str, str]:
    """Forward only the existing allowlist to spawned commands."""
    selected = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
    }
    if "DOCKER_HOST" in os.environ:
        selected["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
    return selected


def _terminate_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        # Popen has no portable process-tree kill. taskkill handles descendants too,
        # which matters when a child inherited the captured stdout pipe.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def capture_process(
    argv: Sequence[str],
    timeout: float,
    limit: int = LIMIT,
    on_stop: Callable[[], None] | None = None,
) -> dict:
    """Capture a command’s output tail while enforcing its deadline.

    Spawn without a shell and forward only allowlisted environment values. On timeout
    or interruption, terminate descendants and invoke the supplied cleanup callback."""
    env = _child_environment()
    popen_options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = subprocess.Popen(
        list(argv),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **popen_options,
    )
    output, shortened, stopped = bytearray(), False, None
    deadline = time.monotonic() + timeout
    output_lock = threading.Lock()
    reader_errors: list[BaseException] = []

    def collect_output() -> None:
        nonlocal shortened
        assert process.stdout is not None
        try:
            while chunk := process.stdout.read1(4096):
                with output_lock:
                    output.extend(chunk)
                    if len(output) > limit:
                        shortened = True
                        del output[: len(output) - limit]
        except BaseException as error:  # noqa: BLE001 - relay reader failures to the owning thread
            reader_errors.append(error)

    reader = threading.Thread(target=collect_output, name="harness-output-reader", daemon=True)
    reader.start()
    try:
        while process.poll() is None or reader.is_alive():
            if reader_errors:
                raise OSError("failed to capture subprocess output") from reader_errors[0]
            if time.monotonic() >= deadline:
                stopped = f"timed out after {timeout}s"
                break
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        if reader_errors:
            raise OSError("failed to capture subprocess output") from reader_errors[0]
    except BaseException:
        stopped = "interrupted"
        raise
    finally:
        if stopped:
            _terminate_tree(process)
            if on_stop:
                on_stop()
        reader.join(timeout=1)
        if process.stdout:
            process.stdout.close()
    with output_lock:
        text = output.decode("utf-8", "replace").replace("\r\n", "\n")
    if shortened:
        text = f"[output shortened: showing the last {limit} bytes]\n" + text
    return {
        "exit_code": process.wait(),
        "output": text,
        "shortened": shortened,
        "stopped": stopped,
    }
