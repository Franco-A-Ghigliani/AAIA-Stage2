"""Deterministic selection and budgeting of model context."""

from __future__ import annotations

import json
import re

from .action_protocol import UNTRUSTED

STOPWORDS = frozenset(
    {"the", "and", "for", "with", "that", "this", "must", "not", "from", "when", "into", "may"}
    | {"only", "are", "was", "has", "have", "you", "use", "add", "new", "any", "all", "src"}
    | {"file", "files", "test", "tests", "after", "before", "existing"}
)
SELECTED_FILES = 4
# Shares of the space left after the trusted messages.
RECENT_SHARE, MAP_SHARE, FILES_SHARE, HISTORY_SHARE = 0.5, 0.1, 0.25, 0.1
SCALES = (1, 0.7, 0.5, 0.3, 0.15, 0.05, 0)


def _message_length(messages: list[dict]) -> int:
    return sum(len(message["content"]) for message in messages)


def _keep_start(text: str, limit: int) -> str:
    """Retain a prefix and its truncation marker within the character budget."""
    if len(text) <= limit:
        return text
    mark = "\n[shortened]"
    if limit <= len(mark):
        return ""
    return text[: limit - len(mark)] + mark


def _keep_end(text: str, limit: int) -> str:
    """Retain a suffix and its truncation marker within the character budget."""
    if len(text) <= limit:
        return text
    mark = "[shortened]\n"
    if limit <= len(mark):
        return ""
    retained = limit - len(mark)
    return mark + text[-retained:]


def _keywords(text: str) -> set[str]:
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    words = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", spaced)} - STOPWORDS
    return {
        w.removesuffix("s") for w in words
    }  # Normalize plurals only; keep the original matching heuristic.


class PromptBuilder:
    """Assemble bounded prompts while retaining complete trusted instructions and task text."""

    def __init__(self, repository, limit: int):
        """Create the manager.

        Args:
            repository: WorkingTree (symbol_outline, read_text).
            limit: Maximum characters of message content per model call.
        """
        self.repository = repository
        self.limit = limit

    def outline_repository(self) -> str:
        """Join the repository symbol summaries into a single context section."""
        return "\n".join(self.repository.symbol_outline())

    def rank_files(self, task: str) -> list[str]:
        """Rank paths and symbols by task-word overlap, breaking ties by path."""
        wanted = _keywords(task)
        candidates: list[tuple[int, str]] = []
        for line in self.repository.symbol_outline():
            path, _, symbols = line.partition(": ")
            score = 3 * len(wanted & _keywords(path)) + len(wanted & _keywords(symbols))
            if not score:
                continue
            candidates.append((-score, path))
        return [path for _score, path in sorted(candidates)[:SELECTED_FILES]]

    def summarize_history(self, messages: list[dict]) -> list[str]:
        """Reduce older tool requests and observations to brief ordered summaries."""
        lines = []
        for message in messages:
            try:
                data = json.loads(message["content"])
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if message["role"] == "assistant" and "tool" in data:
                lines.append(f"requested {data['tool']} {json.dumps(data.get('args'))[:120]}")
            elif message["role"] == "user" and isinstance(data.get("result"), dict):
                result = data["result"]
                detail = "" if result.get("status") == "ok" else f": {result.get('output')}"
                lines.append(f"  -> {result.get('status')}{detail}"[:300])
        return lines

    def _selected_content(self, task: str, room: int) -> dict[str, str]:
        selected = self.rank_files(task)
        each = room // max(1, len(selected))
        return {path: _keep_start(self.repository.read_text(path), each) for path in selected}

    def _context_payload(self, task: str, older: list[dict], room: int) -> str:
        """Encode repository context as untrusted JSON with separate section budgets."""
        return json.dumps(
            {
                "trust": UNTRUSTED,
                "repo_map": _keep_start(self.outline_repository(), int(room * MAP_SHARE)),
                "relevant_files": self._selected_content(task, int(room * FILES_SHARE)),
                "earlier_steps": _keep_end(
                    "\n".join(self.summarize_history(older)), int(room * HISTORY_SHARE)
                ),
            }
        )

    def compose_messages(self, messages: list[dict]) -> list[dict]:
        """Fit recent history and repository context around the unchanged trusted messages.

        Raise ValueError when even the smallest permitted context cannot fit."""
        trusted, history = messages[:2], messages[2:]
        room = self.limit - _message_length(trusted)
        if room <= 0:
            raise ValueError(f"task and instructions exceed the context limit of {self.limit}")
        recent_budget, keep = int(room * RECENT_SHARE), len(history)
        recent: list[dict] = []
        for index in range(len(history) - 1, -1, -1):
            content = history[index]["content"]
            if recent and len(content) > recent_budget:
                break
            text = _keep_end(content, recent_budget)
            recent.insert(0, {**history[index], "content": text})
            recent_budget -= len(text)
            keep = index
        while len(recent) > 1 and recent[0]["role"] != "assistant":  # keep request-result pairs
            recent.pop(0)
            keep += 1
        older = history[:keep]
        for scale in SCALES:
            context = {
                "role": "user",
                "content": self._context_payload(trusted[1]["content"], older, int(room * scale)),
            }
            total = _message_length([*trusted, context, *recent])
            if total > self.limit:
                continue
            return [trusted[0], context, trusted[1], *recent]
        raise ValueError(f"context could not fit the limit of {self.limit}")
