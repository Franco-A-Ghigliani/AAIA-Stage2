"""Disposable Git checkouts and content fingerprints."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def git_output(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Run Git with hooks and filesystem monitoring disabled, reporting failures as RuntimeError."""
    done = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
        check=False,
        env={**os.environ, **env} if env else None,
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def fingerprint(workspace: Path) -> str:
    """Hash tracked and untracked checkout content using an isolated temporary Git index."""
    with tempfile.TemporaryDirectory() as folder:
        env = {"GIT_INDEX_FILE": str(Path(folder) / "index")}
        git_output(workspace, "add", "-A", env=env)
        return git_output(workspace, "write-tree", env=env).strip()


def clone_checkout(source: Path | str, commit: str, dest: Path | str) -> Path:
    """Clone and detach at the requested commit, then remove the clone’s remote."""
    source, dest = Path(source).resolve(), Path(dest).resolve()
    if not (source / ".git").exists():
        raise RuntimeError(f"target clone not found at {source}; see README, Setup")
    dest.parent.mkdir(parents=True, exist_ok=True)
    git_output(dest.parent, "clone", "-q", "--no-hardlinks", str(source), str(dest))
    git_output(dest, "checkout", "-q", "--detach", commit)
    git_output(dest, "remote", "remove", "origin")
    return dest
