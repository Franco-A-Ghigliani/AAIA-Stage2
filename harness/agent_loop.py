"""Execute a bounded sequence of model decisions with durable checkpoints."""

from __future__ import annotations

import json
from collections.abc import Callable

from .action_protocol import (
    SELF_REVIEW,
    SYSTEM_PROMPT,
    TOOL_SCHEMA,
    UNTRUSTED,
    decode_reply,
    validate_request,
)
from .errors import Denied, ToolError

INVALID_REPLY_LIMIT = 5


class _TransientJournal:
    """Provide the journal interface for runs without persistent storage."""

    def record_checkpoint(self, state: dict, action: dict | None = None) -> None:
        return None

    def finish(self, action_id: int | None, result: dict) -> None:
        return None


class TaskAgent:
    """Coordinate model decisions, validated tools, and checkpoint persistence."""

    def __init__(
        self,
        model: Callable[[list[dict]], str],
        repository,
        env,
        max_actions: int = 15,
        emit: Callable[[dict], None] | None = None,
        journal=None,
        context=None,
        max_failed_checks: int | None = None,
        self_reviews: int = 0,
    ):
        """Create a controller.

        Args:
            model: Callable taking the message list and returning one JSON string.
            repository: WorkingTree (enumerate_files, read_text, search_text, replace_text).
            env: ContainerChecks (commands, check, stop).
            max_actions: Maximum model calls; invalid replies count too.
            emit: Optional callback receiving one event dictionary per step.
            journal: Saves checkpoints; ``record_checkpoint(state, action)`` runs before the
                action and ``finish(id, result)`` after it.
            context: Optional PromptBuilder bounding what each model call sees.
            max_failed_checks: Consecutive failed checks before the run stops.
            self_reviews: Times a final answer is sent back for a check against the
                task before it is accepted.
        """
        self.model = model
        self.repository = repository
        self.env = env
        self.max_actions = max_actions
        self.emit = emit or (lambda _event: None)
        self.journal = journal or _TransientJournal()
        self.context = context
        self.max_failed_checks = max_failed_checks
        self.self_reviews = self_reviews
        self.counts = dict.fromkeys(
            ("model_calls", "executed", "denied", "errors", "invalid_replies", "retries"), 0
        )
        self._handlers = {
            "list_files": repository.enumerate_files,
            "read_file": lambda path: repository.read_text(path),
            "search": lambda query: repository.search_text(query),
            "find_symbol": lambda name: repository.locate_symbol(name),
            "dependencies": lambda path: repository.import_relationships(path),
            "git_state": repository.describe_changes,
            "edit_file": lambda path, old, new: repository.replace_text(path, old, new),
            "run_check": lambda name: self._checked_result(name),
        }

    def tool_catalog(self) -> list[dict]:
        """Describe the stable tool protocol with the configured check names."""
        checks = ", ".join(sorted(self.env.commands)) or "(none)"
        return [
            {"name": name, "args": list(args), "description": desc.replace("{checks}", checks)}
            for name, (args, desc) in TOOL_SCHEMA.items()
        ]

    def _checked_result(self, name: str) -> dict:
        result = self.env.check(name)
        if result["status"] != "passed":
            raise ToolError(result)  # failed, stopped or unavailable: visible, not "ok"
        return result

    def dispatch_action(self, action: dict) -> dict:
        """Execute a validated request and convert supported exceptions into observations."""
        try:
            name, args = validate_request(action)
            self.counts["executed"] += 1
            return {"status": "ok", "output": self._handlers[name](**args)}
        except Denied as exc:
            self.counts["denied"] += 1
            return {"status": "denied", "output": str(exc)}
        except ToolError as exc:
            self.counts["errors"] += 1
            return {"status": "error", "output": exc.args[0] if exc.args else str(exc)}
        except OSError as exc:
            self.counts["errors"] += 1
            return {"status": "error", "output": f"{type(exc).__name__}: {exc}"}

    def perform_task(self, task: str, state: dict | None = None) -> dict:
        """Continue from a checkpoint or initialize a task, stopping at the existing limits.

        Calls are counted before contacting the model. Interrupted actions retain their
        pre-action checkpoint so resume never silently replays an uncertain operation."""
        if state:
            messages, failed = state["messages"], state["failed"]
            check_failures, turn = state["failed_checks"], state["turn"]
            invalid_count = state.get("invalid_streak", 0)
            review_count = state.get("reviews", 0)
            self.counts.update(state["counts"])
        else:
            tools = "\n".join(
                f"- {t['name']}({', '.join(t['args'])}): {t['description']}"
                for t in self.tool_catalog()
            )
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT.replace("{tools}", tools)},
                {"role": "user", "content": task},
            ]
            failed: dict[str, object] = {}  # canonical action JSON -> its failed result
            check_failures, turn, invalid_count, review_count = 0, 0, 0, 0

        def fingerprint() -> dict:
            return {
                "turn": turn,
                "counts": self.counts,
                "messages": messages,
                "failed": failed,
                "failed_checks": check_failures,
                "invalid_streak": invalid_count,
                "reviews": review_count,
            }

        def stop(termination: str, **extra) -> dict:
            completion = {"termination": termination, "turns": turn, "counts": self.counts, **extra}
            self.journal.record_checkpoint(fingerprint())
            self.emit({"type": "stop", **completion})
            return completion

        def apply_request(action: dict) -> dict:
            nonlocal check_failures
            key = json.dumps(action, sort_keys=True, default=str)
            if key in failed:
                self.counts["retries"] += 1
                result = {
                    "status": "error",
                    "output": {
                        "error": "repeated request: identical to an earlier request "
                        "that failed, so it was NOT executed again. Change the "
                        "arguments or use a different approach.",
                        "earlier_result": failed[key],
                    },
                }
            else:
                action_id = self.journal.record_checkpoint(fingerprint(), action)
                result = self.dispatch_action(action)
                self.journal.finish(action_id, result)
                if result["status"] != "ok":
                    failed[key] = result["output"]
                elif action["tool"] == "edit_file":
                    failed.clear()  # files changed, so earlier failures may now pass
                if action.get("tool") == "run_check":
                    output = result["output"]
                    if result["status"] == "ok":
                        check_failures = 0
                    elif isinstance(output, dict) and "check" in output:
                        check_failures += 1
            return result

        try:
            while turn < self.max_actions:
                turn += 1
                left = self.max_actions - turn
                self.counts["model_calls"] += 1
                self.journal.record_checkpoint(
                    fingerprint()
                )  # the call counts even if it never returns
                try:
                    prompt = self.context.compose_messages(messages) if self.context else messages
                    reply = self.model(prompt)
                except Exception as exc:  # noqa: BLE001 - any model failure ends the run
                    return stop("model_error", reason=f"{type(exc).__name__}: {exc}")

                messages.append({"role": "assistant", "content": str(reply)})
                try:
                    action = decode_reply(reply)
                except ValueError as exc:
                    self.counts["invalid_replies"] += 1
                    invalid_count += 1
                    self.emit({"type": "invalid_reply", "turn": turn, "error": str(exc)})
                    observation = {"error": str(exc), "actions_left": left}
                else:
                    invalid_count = 0
                    if "final" in action and review_count < self.self_reviews:
                        review_count += 1
                        self.emit({"type": "self_review", "turn": turn})
                        review = {"review": SELF_REVIEW, "actions_left": left}
                        messages.append({"role": "user", "content": json.dumps(review)})
                        continue
                    if "final" in action:
                        self.emit({"type": "final", "turn": turn, "final": action["final"]})
                        return stop("final", final=action["final"])
                    self.emit({"type": "request", "turn": turn, "action": action})
                    result = apply_request(action)
                    self.emit({"type": "result", "turn": turn, "tool": action.get("tool"),
                               "result": result})  # fmt: skip
                    observation = {
                        "tool": action.get("tool"),
                        "trust": UNTRUSTED,
                        "result": result,
                        "actions_left": left,
                    }
                    if not self.context:  # with a context manager the task is always sent
                        observation["task_reminder"] = task
                # Observations go back as user-role data, never as system instructions.
                messages.append({"role": "user", "content": json.dumps(observation, default=str)})
                if invalid_count >= INVALID_REPLY_LIMIT:
                    return stop("retry_limit", reason=f"{invalid_count} invalid replies in a row")
                if self.max_failed_checks and check_failures >= self.max_failed_checks:
                    return stop("retry_limit", reason=f"{check_failures} failed checks in a row")
        except KeyboardInterrupt:
            self.env.stop()
            # No checkpoint: the last one must stay the state from before the interruption.
            completion = {
                "termination": "cancelled",
                "turns": turn,
                "counts": self.counts,
                "reason": "cancelled by user",
            }
            self.emit({"type": "stop", **completion})
            return completion

        return stop("action_limit", reason=f"no final answer after {self.max_actions} actions")
