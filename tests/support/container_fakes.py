"""Reusable fixtures for behavior-focused tests."""

import sys


def docker_stub(tmp_path, body):
    """A stand-in for the docker CLI, to test how its exit codes are classified."""
    path = tmp_path / "fake-docker"
    path.write_text(f"#!{sys.executable}\nimport sys\n{body}\n")
    path.chmod(0o755)
    return str(path)
