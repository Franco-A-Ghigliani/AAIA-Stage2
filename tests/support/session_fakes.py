"""Reusable fixtures for behavior-focused tests."""

import json
import subprocess

import pytest

from harness.errors import ToolError

FIX_OLD = "def total(prices):\n    return sum(prices) + 1\n"


FIX_NEW = "def total(prices):\n    return sum(prices)\n"


@pytest.fixture
def project(tmp_path):
    """A tiny target repository with one commit, plus settings that point at it."""
    target = tmp_path / "target"
    (target / "src").mkdir(parents=True)
    (target / "tests").mkdir()
    (target / "src/shop.py").write_text(FIX_OLD)
    (target / "tests/test_shop.py").write_text("def test_ok():\n    assert True\n")
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "start"],
    ):
        subprocess.run(["git", *args], cwd=target, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=target, check=True, capture_output=True, text=True
    ).stdout.strip()
    (tmp_path / "task").mkdir()
    task_spec = {
        "project": "shop",
        "task": "fix",
        "source": str(target),
        "commit": commit,
        "writable": ["src/"],
        "image": "img:1",
        "env": {},
        "commands": {"unit_tests": ["pytest", "tests"]},
        "verification": {
            "acceptance": ["pytest", "/acceptance"],
            "unit_tests": ["pytest", "tests"],
        },
        "acceptance": str(tmp_path / "task"),
        "request": "fix total()",
        "limits": {"max_actions": 6, "output_bytes": 12000, "command_timeout": 5},
    }
    return tmp_path, task_spec


class FakeEnv:
    """Stands in for the container: acceptance passes only if the fix is in the workspace."""

    unavailable = False

    def __init__(self, workspace, image, commands, **kwargs):
        self.workspace, self.commands, self.kwargs = workspace, commands, kwargs

    def check(self, name):
        if name not in self.commands:
            raise ToolError(f"unknown check: {name!r}")
        if self.unavailable:
            return {
                "check": name,
                "status": "unavailable",
                "exit_code": 125,
                "output": "Cannot connect to the Docker daemon",
                "shortened": False,
            }
        fixed = (self.workspace / "src/shop.py").read_text() == FIX_NEW
        ok = fixed or name != "acceptance"
        return {
            "check": name,
            "status": "passed" if ok else "failed",
            "exit_code": int(not ok),
            "output": "1 passed" if ok else "E   assert 4 == 3\n1 failed",
            "shortened": False,
        }

    def stop(self):
        pass


def tool_request(tool, **args):
    return json.dumps({"tool": tool, "args": args})
