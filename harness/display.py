"""Text presentation for command results and live events."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

STATUS_LABELS = {
    "passed": "PASS",
    "failed": "FAIL",
    "stopped": "STOPPED",
    "unavailable": "UNAVAILABLE",
}


def format_event(event: dict) -> str | None:
    """Format visible progress events and suppress events without a display representation."""
    kind, turn = event.get("type"), event.get("turn")
    if kind == "started":
        return (
            f"session:   {event['session']}\nworkspace: {event['workspace']}\n"
            f"commit:    {event['commit']}\n"
        )
    if kind == "request":
        args = event["action"].get("args")
        target = next(iter(args.values()), "") if isinstance(args, dict) and args else ""
        return f"[{turn}] {event['action'].get('tool')} {str(target)[:70]}".rstrip()
    if kind == "result":
        result = event["result"]
        detail = "" if result["status"] == "ok" else f": {str(result['output'])[:160]}"
        return f"      -> {result['status']}{detail}"
    if kind == "invalid_reply":
        return f"[{turn}] invalid reply: {event['error'][:120]}"
    if kind == "self_review":
        return f"[{turn}] final answer sent back for self-review"
    if kind == "final":
        return f"[{turn}] model finished"
    if kind == "verifying":
        return "\nverifying..."
    return None


def render_report(completion: dict, report: dict, out: Callable[[str], None]) -> None:
    """Print the outcome, changes, check evidence, unfinished work, and final verdict."""
    counts = completion["counts"]
    out("\n== Run ==")
    out(f"termination: {completion['termination']} after {completion['turns']} model calls")
    if "reason" in completion:
        out(f"reason: {completion['reason']}")
    out(
        "actions executed: {executed} | denied: {denied} | errors: {errors} | "
        "invalid replies: {invalid_replies} | repeated requests not re-run: {retries}".format(
            **counts
        )
    )
    if "final" in completion:
        # The claim is the model's own text; the checks below are what was verified.
        out(f"model claims (unverified): {completion['final']}")
    out("\n== Changed files ==")
    for name in report["changed_files"] or ["(none)"]:
        out(f"  {name}")
    for name in report["out_of_scope"]:
        out(f"  OUT OF SCOPE: {name}")
    out("\n== Diff ==")
    out(report["diff"].rstrip() or "(empty)")
    out("\n== Checks (run by the harness in the container) ==")
    for check in report["checks"] or []:
        out(
            f"  [{STATUS_LABELS[check['status']]}] {check['check']} (exit code {check['exit_code']})"
        )
        if check["status"] != "passed":
            for line in check["output"].rstrip().splitlines()[-25:]:
                out(f"      {line}")
    if not report["checks"]:
        out("  (not run)")
    out("\n== Unfinished ==")
    for item in report.get("unfinished") or ["(nothing)"]:
        out(f"  {item}")
    out(f"\nVERDICT: {'PASSED' if report['passed'] else 'NOT PASSED'}")


def event_printer(out: Callable[[str], None]) -> Callable[[dict], None]:
    def event_sink(event: dict) -> None:
        if line := format_event(event):
            out(line)

    return event_sink


def finish_command(result: dict, out: Callable[[str], None]) -> int:
    if result["status"] == "paused":
        out("PAUSED, nothing was run:")
        for problem in result["problems"]:
            out(f"  {problem}")
        return result["code"]
    render_report(result["outcome"], result["report"], out)
    out(f"session: {result['session']} ({result['status']})")
    out(f"trace: {Path(result['run_dir']) / 'trace.jsonl'}")
    return result["code"]


def render_session(info: dict, out: Callable[[str], None]) -> int:
    task_spec = info["spec"]
    out(f"session: {info['session']}\nstatus:  {info['status']}")
    if info["reason"]:
        out(f"reason:  {info['reason']}")
    out(f"project: {task_spec['project']} at {task_spec['commit']}\ntask:    {task_spec['task']}")
    out(f"model calls used: {info['turn']} of {task_spec['limits']['max_actions']}")
    out(f"\n== Request ==\n{task_spec['request'].rstrip()}\n\n== Steps ==")
    for entry in info["actions"]:
        action = entry["action"]
        args = action.get("args")
        target = next(iter(args.values()), "") if isinstance(args, dict) and args else ""
        ended = (entry["result"] or {}).get("status", entry["status"].upper())
        out(f"[{entry['turn']}] {action.get('tool')} {str(target)[:70]} -> {ended}")
    if info["result"]:
        render_report(info["result"]["outcome"], info["result"]["report"], out)
    return 0
