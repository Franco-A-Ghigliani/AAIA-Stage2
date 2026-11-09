"""Session lifecycle orchestration independent of terminal presentation."""

from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from .agent_loop import TaskAgent
from .assessment import assess_checkout, pending_items, read_diff
from .checkout import clone_checkout, fingerprint, git_output
from .containers import ContainerChecks
from .journal import DATABASE_NAME, SessionJournal
from .prompt_context import PromptBuilder
from .repository import WorkingTree

# A change to any of these after a session started is a permission change.
IMMUTABLE_FIELDS = ("project", "source", "commit", "writable", "image", "commands", "verification")


def prepare_task(
    settings: dict, base: Path, project: str, task: str, request: str | None = None
) -> dict:
    """Resolve an allowed project and task into the specification stored with a session."""
    projects = settings["projects"]
    if project not in projects:
        raise RuntimeError(f"project {project!r} is not allowed; allowed: {sorted(projects)}")
    config = projects[project]
    if task not in config["tasks"]:
        raise RuntimeError(f"unknown task {task!r}; tasks: {sorted(config['tasks'])}")
    chosen = config["tasks"][task]
    return {
        "project": project,
        "task": task,
        "source": str(base / config["source"]),
        "commit": config["commit"],
        "writable": config["writable"],
        "image": config["image"],
        "env": config.get("env", {}),
        "commands": config["commands"],
        "verification": config["verification"],
        "acceptance": str(base / chosen["acceptance"]),
        "request": request or (base / chosen["request"]).read_text(encoding="utf-8"),
        "limits": settings["limits"],
    }


def _process_exists(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)  # PID reuse is intentionally not distinguished here.
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _WorkspaceJournal:
    """Attach a working-tree fingerprint to each stored checkpoint."""

    def __init__(self, store: SessionJournal, workspace: Path):
        self.store, self.workspace = store, workspace

    def record_checkpoint(self, state: dict, action: dict | None = None) -> int | None:
        return self.store.record_checkpoint(state, fingerprint(self.workspace), action)

    def finish(self, action_id: int | None, result: dict) -> None:
        self.store.complete_action(action_id, result)


def _execute_session(
    store: SessionJournal,
    task_spec: dict,
    workspace: Path,
    model: Callable[[list[dict]], str],
    event_sink: Callable[[dict], None],
    checks_factory: Callable[..., ContainerChecks],
    limits: dict,
    state: dict | None = None,
) -> dict:
    """Connect the agent, journal, trace, and final checks for a new or resumed session."""
    session_dir = store.path.parent
    repository = WorkingTree(workspace, task_spec["writable"], limits["output_bytes"])

    def make_env(commands: dict) -> ContainerChecks:
        return checks_factory(
            workspace,
            task_spec["image"],
            commands,
            mounts={"/acceptance": Path(task_spec["acceptance"])},
            env=task_spec["env"],
            timeout=limits["command_timeout"],
            limit=limits["output_bytes"],
        )

    store.record_status("running", pid=os.getpid())
    with (session_dir / "trace.jsonl").open("a", encoding="utf-8") as log:

        def emit(event: dict) -> None:
            log.write(json.dumps({"time": time.time(), **event}, default=str) + "\n")
            log.flush()
            event_sink(event)

        context = None
        if limits.get("context_chars"):
            context = PromptBuilder(repository, limits["context_chars"])
        controller = TaskAgent(
            model,
            repository,
            make_env(task_spec["commands"]),
            limits["max_actions"],
            emit,
            _WorkspaceJournal(store, workspace),
            context,
            limits.get("max_failed_checks"),
            limits.get("self_reviews", 0),
        )
        completion = controller.perform_task(task_spec["request"], state)
        cancelled = completion["termination"] == "cancelled"
        try:
            # A cancelled run starts no further container.
            commands = {} if cancelled else task_spec["verification"]
            event_sink({"type": "verifying", "checks": sorted(commands)})
            report = assess_checkout(workspace, make_env(commands), repository.allows_write)
        except KeyboardInterrupt:
            completion.update(termination="cancelled", reason="cancelled during verification")
            report = assess_checkout(workspace, make_env({}), repository.allows_write)
        report["unfinished"] = pending_items(completion, report, store.action_history("uncertain"))
        emit({"type": "verification", **report})
    status = _completion_status(completion, report)
    result = {
        "outcome": completion,
        "report": report,
        "run_dir": str(session_dir),
        "session": session_dir.name,
    }
    store.record_result(status, result)
    return {**result, "status": status, "code": 0 if report["passed"] else 1}


def _completion_status(completion: dict, report: dict) -> str:
    """Resolve the persisted status independently of the model's claims."""
    termination = completion["termination"]
    if termination == "cancelled":
        return "cancelled"
    if termination != "final":
        return "stopped"
    return "completed" if report["passed"] else "failed"


def begin_session(
    task_spec: dict,
    model: Callable[[list[dict]], str],
    event_sink: Callable[[dict], None],
    checks_factory: Callable[..., ContainerChecks],
    sessions_dir: Path,
) -> dict:
    """Create an isolated checkout and journal, then run and persist the task outcome."""
    session_dir = sessions_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    workspace = clone_checkout(task_spec["source"], task_spec["commit"], session_dir / "workspace")
    store = SessionJournal.create(session_dir / DATABASE_NAME, task_spec)
    try:
        event_sink({"type": "started", "session": session_dir.name, "workspace": str(workspace),
                  "commit": task_spec["commit"]})  # fmt: skip
        return _execute_session(
            store, task_spec, workspace, model, event_sink, checks_factory, task_spec["limits"]
        )
    finally:
        store.close()


def inspect_resume(store: SessionJournal, current: dict | None) -> tuple[list[str], list[str]]:
    """Separate non-overridable resume blockers from state changes requiring acceptance."""
    metadata, record_checkpoint = store.metadata(), store.last_checkpoint()
    task_spec, workspace = metadata["spec"], store.path.parent / "workspace"
    blocked, confirm = [], []
    if metadata["status"] == "completed":
        blocked.append("the session already completed")
    if metadata["status"] in ("running", "created") and _process_exists(metadata["pid"]):
        blocked.append(f"the session is still running as process {metadata['pid']}")
    if current is None:
        blocked.append("the settings no longer allow this project or task")
    else:
        blocked += [
            f"{key} changed since the session started"
            for key in IMMUTABLE_FIELDS
            if task_spec[key] != current[key]
        ]
    if record_checkpoint is None:
        blocked.append("the session has no checkpoint")
    elif git_output(workspace, "rev-parse", "HEAD").strip() != task_spec["commit"]:
        blocked.append("the workspace is no longer on the starting commit")
    elif fingerprint(workspace) != record_checkpoint["tree"]:
        confirm.append("the working copy differs from the last checkpoint")
    confirm += [
        f"uncertain interrupted action, not replayed: {entry['action']}"
        for entry in store.action_history("uncertain")
    ]
    return blocked, confirm


def continue_session(
    session_dir: Path,
    current: dict | None,
    model: Callable[[list[dict]], str],
    event_sink: Callable[[dict], None],
    checks_factory: Callable[..., ContainerChecks],
    accept_state: bool = False,
) -> dict:
    """Resume with consumed counters and non-increasing limits, or return a paused result."""
    store = SessionJournal(session_dir / DATABASE_NAME)
    try:
        blocked, confirm = inspect_resume(store, current)
        if blocked or (confirm and not accept_state):
            problems = blocked + confirm
            if store.metadata()["status"] != "completed":
                store.record_status("paused", reason="; ".join(problems))
            return {"status": "paused", "problems": problems, "code": 3}
        task_spec = store.metadata()["spec"]
        state = store.last_checkpoint()["state"]
        limits = dict(task_spec["limits"])
        for key in ("max_actions", "max_failed_checks", "context_chars", "self_reviews"):
            if key in limits and key in current["limits"]:
                limits[key] = min(limits[key], current["limits"][key])  # never widened
        note = "The session was resumed by the harness. Re-read files before editing them."
        if confirm:
            store.discard_uncertain()
            note += " A person accepted the current files; an interrupted action's effect is "
            note += "unknown and it was not repeated."
        state["messages"].append({"role": "user", "content": json.dumps({"harness_note": note})})
        return _execute_session(
            store,
            task_spec,
            session_dir / "workspace",
            model,
            event_sink,
            checks_factory,
            limits,
            state,
        )
    finally:
        store.close()


def request_cancellation(session_dir: Path) -> bool:
    """Signal a live running session and report whether cancellation was requested."""
    store = SessionJournal(session_dir / DATABASE_NAME)
    try:
        metadata = store.metadata()
    finally:
        store.close()
    if metadata["status"] != "running" or not _process_exists(metadata["pid"]):
        return False
    os.kill(metadata["pid"], 2)  # SIGINT: the process stops its container and saves its state
    return True


def inspect_session(session_dir: Path) -> dict:
    """Read persisted session details and the checkout diff, detecting interrupted processes."""
    store = SessionJournal(session_dir / DATABASE_NAME)
    try:
        metadata = store.metadata()
        record_checkpoint = store.last_checkpoint()
        if metadata["status"] == "running" and not _process_exists(metadata["pid"]):
            metadata["status"] = "interrupted"
        return {
            **metadata,
            "session": session_dir.name,
            "turn": record_checkpoint["turn"] if record_checkpoint else 0,
            "counts": record_checkpoint["state"]["counts"] if record_checkpoint else {},
            "actions": store.action_history(),
            "diff": read_diff(session_dir / "workspace"),
        }
    finally:
        store.close()


def enumerate_sessions(sessions_dir: Path) -> list[dict]:
    """Summarize saved sessions in directory order with their task, status, and turn."""
    sessions = []
    for path in sorted(sessions_dir.glob(f"*/{DATABASE_NAME}")):
        info = inspect_session(path.parent)
        sessions.append(
            {
                "session": info["session"],
                "project": info["spec"]["project"],
                "task": info["spec"]["task"],
                "status": info["status"],
                "turn": info["turn"],
            }
        )
    return sessions
