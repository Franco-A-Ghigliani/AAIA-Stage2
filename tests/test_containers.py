"""Execution: exit codes, output limit, timeouts, and the container command line."""

import os
import shutil
import subprocess
import sys
import time

import pytest

from harness.containers import ContainerChecks
from harness.errors import ToolError
from harness.processes import capture_process
from tests.support.container_fakes import docker_stub


def py(code):
    return [sys.executable, "-c", code]


def test_nonzero_exit_code_and_its_output_stay_visible():
    result = capture_process(py("import sys; print('2 failed'); sys.exit(3)"), timeout=10)
    assert result == {"exit_code": 3, "output": "2 failed\n", "shortened": False, "stopped": None}


def test_large_output_is_bounded_and_marked_as_shortened():
    result = capture_process(py("print('x' * 50000); print('THE END')"), timeout=10, limit=1000)
    assert result["shortened"] and result["output"].startswith("[output shortened")
    assert len(result["output"]) < 1100
    assert result["output"].endswith("THE END\n")  # the end is kept: test summaries live there


def test_stuck_command_is_stopped_with_its_child_processes(tmp_path):
    marker = tmp_path / "late-write.txt"
    child = f"import time; time.sleep(2); open({str(marker)!r}, 'w').write('x')"
    code = (
        f"import subprocess, sys, time; print('before timeout', flush=True); "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
    )
    stopped = []
    started = time.monotonic()
    result = capture_process(py(code), timeout=0.5, on_stop=lambda: stopped.append(True))
    assert time.monotonic() - started < 5
    assert "before timeout" in result["output"]
    assert "timed out" in result["stopped"] and stopped == [True]
    time.sleep(2.5)
    assert not marker.exists()  # the child was killed too: no write left behind


def make_env(tmp_path, **kwargs):
    return ContainerChecks(
        tmp_path,
        "img:1",
        {"unit_tests": ["python", "-m", "pytest", "tests/unit"]},
        mounts={"/acceptance": tmp_path / "task"},
        env={"PYTHONPATH": "/work/src"},
        **kwargs,
    )


def test_container_command_is_isolated_and_mounts_are_read_only(tmp_path):
    argv = make_env(tmp_path).container_arguments("unit_tests", "harness-1")
    joined = " ".join(argv)
    assert argv[:2] == ["docker", "run"] and argv[-5:] == [
        "img:1",
        "python",
        "-m",
        "pytest",
        "tests/unit",
    ]
    assert "--network none" in joined and "--read-only" in joined and "--cap-drop ALL" in joined
    assert f"{tmp_path.resolve()}:/work:ro" in argv
    assert f"{(tmp_path / 'task').resolve()}:/acceptance:ro" in argv
    assert [a for a in argv if a.startswith("-e")] == ["-e", "-e"]  # only the configured env
    assert "PYTHONPATH=/work/src" in argv and "HOME=/tmp" in argv


def test_only_configured_checks_can_run(tmp_path):
    with pytest.raises(ToolError, match="unknown check: 'rm -rf /'"):
        make_env(tmp_path).check("rm -rf /")


def test_missing_container_runtime_is_reported_as_unavailable(tmp_path):
    result = make_env(tmp_path, docker="no-such-docker-binary").check("unit_tests")
    assert result["status"] == "unavailable" and result["exit_code"] is None
    assert "not installed" in result["output"]


@pytest.mark.parametrize("code, status", [(0, "passed"), (1, "failed"), (125, "unavailable")])
@pytest.mark.skipif(os.name == "nt", reason="fake_docker uses a POSIX executable script")
def test_exit_codes_are_classified(tmp_path, code, status):
    docker = docker_stub(tmp_path, f"print('ran'); sys.exit({code})")
    result = make_env(tmp_path, docker=docker).check("unit_tests")
    assert (result["status"], result["exit_code"], result["output"]) == (status, code, "ran\n")


@pytest.mark.skipif(os.name == "nt", reason="fake_docker uses a POSIX executable script")
def test_timed_out_check_is_stopped_and_kills_the_container(tmp_path):
    log = tmp_path / "docker-calls.txt"
    docker = docker_stub(
        tmp_path,
        f"import time\nopen({str(log)!r}, 'a').write(' '.join(sys.argv[1:3]) + '\\n')\n"
        "time.sleep(0 if sys.argv[1] == 'kill' else 30)",
    )
    result = make_env(tmp_path, docker=docker, timeout=0.5).check("unit_tests")
    assert result["status"] == "stopped" and "timed out" in result["output"]
    calls = log.read_text().splitlines()
    assert calls[0] == "run --rm" and calls[1].startswith("kill harness-")


needs_docker = pytest.mark.skipif(
    not shutil.which("docker")
    or subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=False).returncode
    != 0,
    reason="docker daemon is not running",
)


@needs_docker
def test_real_container_has_no_network_and_cannot_write_to_the_workspace(tmp_path):
    (tmp_path / "a.txt").write_text("keep\n")
    code = (
        "import socket, sys\n"
        "try:\n    open('/work/a.txt', 'w').write('changed'); print('WROTE')\n"
        "except OSError as e:\n    print('write blocked')\n"
        "try:\n    socket.create_connection(('1.1.1.1', 53), 2); print('NETWORK')\n"
        "except OSError:\n    print('network blocked')\n"
    )
    env = ContainerChecks(tmp_path, "python:3.11-slim", {"probe": ["python", "-c", code]})
    result = env.check("probe")
    if result["status"] == "unavailable":
        pytest.skip(f"image not available: {result['output'][:100]}")
    assert result["output"] == "write blocked\nnetwork blocked\n"
    assert result["status"] == "passed"
    assert (tmp_path / "a.txt").read_text() == "keep\n"
