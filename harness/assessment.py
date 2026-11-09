"""Independent verification and unfinished-work reporting."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .checkout import git_output

DIFF_CHARS = 20_000


def modified_paths(workspace: Path) -> list[str]:
    """Extract sorted changed and untracked paths from Git’s porcelain status."""
    status = git_output(workspace, "status", "--porcelain", "--untracked-files=all")
    return sorted(line[3:] for line in status.splitlines() if line.strip())


def read_diff(workspace: Path, limit: int = DIFF_CHARS) -> str:
    """Read the diff against HEAD, applying the existing prefix limit and marker."""
    diff = git_output(workspace, "diff", "--no-ext-diff", "--no-textconv", "HEAD")
    if len(diff) > limit:
        return diff[:limit] + f"\n[diff shortened: showing the first {limit} characters]"
    return diff


def collect_checks(env) -> list[dict]:
    """Execute the verification checks in their configured order."""
    return [env.check(name) for name in env.commands]


def assess_checkout(workspace: Path, env, allows_write: Callable[[str], bool]) -> dict:
    """Combine check results and scope violations into the harness-owned verdict."""
    checks = collect_checks(env)
    files = modified_paths(workspace)  # after the checks, so nothing they did is missed
    out_of_scope = [f for f in files if not allows_write(f)]
    passed = bool(checks) and all(check["status"] == "passed" for check in checks)
    return {
        "checks": checks,
        "changed_files": files,
        "out_of_scope": out_of_scope,
        "diff": read_diff(workspace),
        "passed": passed and not out_of_scope,
    }


def pending_items(completion: dict, report: dict, uncertain: list[dict]) -> list[str]:
    """Describe incomplete execution, uncertain actions, failed checks, and scope violations."""
    items = []
    if completion["termination"] != "final":
        reason = completion.get("reason", "the model did not finish")
        items.append(f"run ended by {completion['termination']}: {reason}")
    items += [f"uncertain interrupted action: {entry['action']}" for entry in uncertain]
    if not report["checks"]:
        items.append("verification checks were not run")
    items += [
        f"check {c['check']}: {c['status']}" for c in report["checks"] if c["status"] != "passed"
    ]
    items += [f"change outside the allowed scope: {name}" for name in report["out_of_scope"]]
    return items
