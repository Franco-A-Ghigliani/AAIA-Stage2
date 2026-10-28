"""Command-line interface for the coding harness."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from coding_harness.context import DEFAULT_CONTEXT_BUDGET
from coding_harness.controller import SessionController
from coding_harness.sessions import SessionError, SessionStore, default_session_dir


MODEL_MAX_TURNS = 15


def build_parser():
    parser = argparse.ArgumentParser(description="Run the disposable coding harness.")
    parser.add_argument("--root", help="target Git repository")
    parser.add_argument("--task", help="feature or bug-fix request")
    parser.add_argument("--max-turns", type=int, default=MODEL_MAX_TURNS,
                        help="cumulative model-call limit for this session")
    parser.add_argument("--allow-change", action="append", default=None,
                        help="allowed relative file path or glob; repeat to add paths")
    parser.add_argument("--check", action="append", default=None,
                        help="contained acceptance or regression command; repeat as needed")
    parser.add_argument("--protected-check", action="append", default=None,
                        help="read-only directory mounted at /protected/N for checks")
    parser.add_argument("--context-budget-chars", type=int, default=DEFAULT_CONTEXT_BUDGET)
    parser.add_argument("--session-dir", default=str(default_session_dir()),
                        help="persistent session store directory")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--resume", metavar="SESSION_ID", help="resume a saved session")
    modes.add_argument("--list-sessions", action="store_true", help="list saved sessions")
    modes.add_argument("--review", metavar="SESSION_ID", help="show a saved diff and check results")
    return parser


def ask_approval(command, cwd):
    """Human approval for one Bash command; only an exact 'y' approves."""
    print(f"\n[bash request]\n  command: {command}\n  cwd:     {cwd}", file=sys.stderr)
    try:
        answer = input("Approve this command only? y/n: ")
    except EOFError:
        answer = ""
    return answer == "y"


def _print_checks(results):
    print("Checks:")
    if not results:
        print("  unavailable: no acceptance or regression checks configured")
        return
    for result in results:
        print(f"  {result['status']}: {result['command']}")
        if result.get("exit_code") is not None:
            print(f"    exit code: {result['exit_code']}")
        if result.get("output"):
            print("    output:")
            for line in result["output"].splitlines()[:30]:
                print(f"      {line}")
        if result.get("reason"):
            print(f"    {result['reason']}")


def _list_sessions(store):
    sessions = store.list_sessions()
    if not sessions:
        print("No saved sessions.")
        return 0
    for item in sessions:
        print(f"{item['id']}  {item['status']}  "
              f"{item['consumed_turns']}/{item['max_turns']} turns")
        print(f"  {item['task'][:160]}")
    return 0


def _review_session(store, session_id):
    manifest = store.load(session_id)
    print(f"session: {session_id}\nstatus: {manifest['status']}")
    print(f"repository: {manifest['repository']}\nstarting commit: {manifest['starting_commit']}")
    print(f"allowed changes: {', '.join(manifest['allowed_changes'])}")
    print(f"model calls: {manifest['consumed_turns']}/{manifest['max_turns']}")
    print(f"saved conversation messages: {len(manifest['history'])}")
    if manifest.get("pending_action"):
        print(f"uncertain interrupted action: {json.dumps(manifest['pending_action'], default=str)}")
    _print_checks(manifest.get("check_results", []))
    for run in manifest.get("check_history", []):
        print(f"check cycle {run['cycle']}:")
        for check in run["results"]:
            print(f"  {check['status']}: {check['command']}")
            if check.get("reason"):
                print(f"    {check['reason']}")
    events_path = store.session_path(session_id) / "events.jsonl"
    if events_path.is_file():
        print("\nRecent progress:")
        try:
            lines = events_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            events = []
        else:
            events = []
            for line in lines:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        for event in events[-40:]:
            kind = event.get("type", "event")
            details = []
            if event.get("turn") is not None:
                details.append(f"turn {event['turn']}")
            if event.get("tool"):
                details.append(f"tool={event['tool']}")
            result = event.get("result")
            if isinstance(result, dict) and result.get("status"):
                details.append(f"result={result['status']}")
            if event.get("termination"):
                details.append(f"termination={event['termination']}")
            if event.get("error"):
                details.append(f"error={str(event['error'])[:180]}")
            if event.get("approved") is not None:
                details.append(f"approved={event['approved']}")
            print(f"  {kind}" + (": " + ", ".join(details) if details else ""))
    result_dir = manifest.get("latest_results")
    if result_dir:
        diff = Path(result_dir) / "changes.diff"
        print(f"\ndiff: {diff}")
        if diff.is_file():
            print(diff.read_text(encoding="utf-8"))
    return 0


def _print_result(result):
    print(f"\nsession: {result['session']}")
    print(f"status: {result['status']}")
    outcome = result["outcome"]
    if outcome.get("reason"):
        print(outcome["reason"])
    if "final" in outcome:
        print(f"Model claims (unverified): {outcome['final']}")
    _print_checks(result["check_results"])
    print(f"Changed files: {len(result['changes'])}")
    for change in result["changes"]:
        print(f"  {change['status']}: {change['path']}")
    print(f"diff: {result['result_dir'] / 'changes.diff'}")


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        store = SessionStore(args.session_dir)
        if args.list_sessions:
            return _list_sessions(store)
        if args.review:
            return _review_session(store, args.review)

        def progress(event):
            payload = json.dumps(event, default=str, ensure_ascii=False)
            print(f"[{event.get('type')}] {payload[:400]}", file=sys.stderr)

        controller = SessionController(store, approve=ask_approval, progress=progress)
        if args.resume:
            manifest = store.load(args.resume)
            lock = store.acquire(args.resume)
            try:
                manifest, context, initialize = controller.prepare_resume(args.resume)
                result = controller.execute(manifest, context, initialize)
            finally:
                store.release(lock)
        else:
            if not args.root or not args.task:
                parser.error("starting a task requires --root and --task")
            manifest, context, initialize = controller.create_session(
                root=args.root, task=args.task, max_turns=args.max_turns,
                allowed_changes=args.allow_change, checks=args.check,
                protected_checks=args.protected_check,
                context_budget_chars=args.context_budget_chars,
            )
            lock = store.acquire(manifest["id"])
            try:
                result = controller.execute(manifest, context, initialize)
            finally:
                store.release(lock)
        _print_result(result)
        return result["exit_code"]
    except (SessionError, OSError, subprocess.SubprocessError, RuntimeError,
            ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
