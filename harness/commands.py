"""Command parsing and dispatch for the python -m harness entry point."""

from __future__ import annotations

import argparse
import signal
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv

from . import application
from .containers import ContainerChecks
from .display import event_printer, finish_command, render_session
from .providers import create_client

PROJECT = Path(__file__).resolve().parents[1]


def read_settings(path: Path | None = None) -> tuple[dict, Path]:
    """Read the selected TOML file and return the base directory for its relative paths."""
    if path is None:
        local = PROJECT / "settings.toml"
        path = local if local.exists() else PROJECT / "settings.example.toml"
    with Path(path).open("rb") as handle:
        return tomllib.load(handle), Path(path).resolve().parent


def launch_session(
    task_spec: dict,
    model: Callable[[list[dict]], str],
    sessions_dir: Path,
    out: Callable[[str], None] = print,
    checks_factory: Callable[..., ContainerChecks] = ContainerChecks,
) -> int:
    """Start a task with live output and render its final report and exit status."""
    result = application.begin_session(
        task_spec, model, event_printer(out), checks_factory, sessions_dir
    )
    return finish_command(result, out)


def _current_task(settings: dict, base: Path, task_spec: dict) -> dict | None:
    try:
        return application.prepare_task(
            settings, base, task_spec["project"], task_spec["task"], task_spec["request"]
        )
    except RuntimeError:
        return None


def _interrupt(_signum, _frame) -> None:
    raise KeyboardInterrupt


def argument_parser() -> argparse.ArgumentParser:
    """Declare the existing commands, options, defaults, and help text."""
    root = argparse.ArgumentParser(prog="harness", description="Stage 2 coding harness")
    root.add_argument("--settings", type=Path, help="settings file (default: settings.toml)")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("projects", help="list allowed projects and their tasks")
    commands.add_parser("models", help="list the models the LiteLLM key may use")
    start = commands.add_parser("start", help="start a task in a new session")
    start.add_argument("--project", default="cosmicpython", help="allowed project")
    start.add_argument("--task", default="cancel-allocation", help="task name in the settings")
    start.add_argument("--request", help="request text (default: the task's request file)")
    start.add_argument("--model", help="model name (overrides the settings)")
    start.add_argument("--max-actions", type=int, help="action limit (overrides the settings)")
    start.add_argument(
        "--baseline", action="store_true", help="no model: verify the unchanged starting commit"
    )
    commands.add_parser("list", help="list saved sessions")
    for name, text in (
        ("show", "show a saved session"),
        ("cancel", "cancel a running session"),
        ("diff", "show a session's diff"),
    ):
        commands.add_parser(name, help=text).add_argument("session")
    resume = commands.add_parser("resume", help="continue a session from its last checkpoint")
    resume.add_argument("session")
    resume.add_argument("--model", help="model name (overrides the settings)")
    resume.add_argument(
        "--accept-state",
        action="store_true",
        help="I reviewed the files: continue despite a mismatch or an uncertain action",
    )
    return root


def _locate_session(sessions_dir: Path, session: str) -> Path:
    if Path(session).name != session or not (sessions_dir / session).is_dir():
        raise RuntimeError(f"unknown session: {session}")
    return sessions_dir / session


def _route_command(args: argparse.Namespace) -> int:
    settings, base = read_settings(args.settings)
    sessions_dir = base / "runs"
    if args.command == "projects":
        for name, project in settings["projects"].items():
            print(f"{name}: tasks {sorted(project['tasks'])}, writable {project['writable']}")
        return 0
    if args.command == "models":
        print(
            "\n".join(
                create_client({**settings["model"], "provider": "litellm"}, "-").available_models()
            )
        )
        return 0
    if args.command == "list":
        for item in application.enumerate_sessions(sessions_dir):
            print("{session}  {status:<11} {project}/{task}  turn {turn}".format(**item))
        return 0
    if args.command == "start":
        task_spec = application.prepare_task(settings, base, args.project, args.task, args.request)
        if args.max_actions:
            task_spec["limits"] = {**task_spec["limits"], "max_actions": args.max_actions}
        if args.baseline:
            task_spec["limits"] = {**task_spec["limits"], "self_reviews": 0}  # nothing to review
            model = lambda _messages: '{"final": "baseline: no model was called"}'
        else:
            model = create_client(settings["model"], args.model)
        return launch_session(task_spec, model, sessions_dir)
    session_dir = _locate_session(sessions_dir, args.session)
    if args.command == "show":
        return render_session(application.inspect_session(session_dir), print)
    if args.command == "diff":
        print(application.inspect_session(session_dir)["diff"].rstrip() or "(empty)")
        return 0
    if args.command == "cancel":
        stopped = application.request_cancellation(session_dir)
        print("cancel requested" if stopped else "the session is not running")
        return 0 if stopped else 1
    task_spec = application.inspect_session(session_dir)["spec"]
    result = application.continue_session(
        session_dir,
        _current_task(settings, base, task_spec),
        create_client(settings["model"], args.model),
        event_printer(print),
        ContainerChecks,
        args.accept_state,
    )
    return finish_command(result, print)


def entrypoint(argv: list[str] | None = None) -> int:
    """Load environment settings, install interrupt handlers, and dispatch one command."""
    load_dotenv(PROJECT / ".env")
    args = argument_parser().parse_args(argv)
    # Both signals stop the run the same way, including when started in the background.
    signal.signal(signal.SIGINT, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        return _route_command(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
