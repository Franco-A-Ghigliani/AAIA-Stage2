"""Transactional session persistence and checkpoint recovery."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

DATABASE_NAME = "session.db"
SCHEMA = """
CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK (id = 1), spec TEXT, status TEXT, pid INTEGER,
                   reason TEXT, result TEXT);
CREATE TABLE checkpoints (seq INTEGER PRIMARY KEY, time REAL, turn INTEGER, tree TEXT, state TEXT);
CREATE TABLE actions (seq INTEGER PRIMARY KEY, time REAL, turn INTEGER, action TEXT, status TEXT,
                      result TEXT);
"""


def dumps(value: object) -> str:
    """Encode journal values as JSON, stringifying values without native JSON representations."""
    return json.dumps(value, default=str)


def _optional_json(value: str | None):
    """Decode nullable result columns using the original empty-value convention."""
    return json.loads(value) if value else None


class SessionJournal:
    """Persist task metadata, checkpoints, and ordered actions in a per-session SQLite file.

    Insert an uncertain action in the same transaction as its checkpoint; only an
    explicit completion records its result."""

    def __init__(self, path: Path | str):
        """Open an existing store.

        Raises:
            RuntimeError: There is no session at this path.
        """
        self.path = Path(path)
        if not self.path.exists():
            raise RuntimeError(f"no session at {self.path}")
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row

    @classmethod
    def create(cls, path: Path | str, task_spec: dict) -> SessionJournal:
        """Create a store holding the immutable task details."""
        with sqlite3.connect(path) as db:
            db.executescript(SCHEMA)
            db.execute(
                "INSERT INTO meta (id, spec, status) VALUES (1, ?, 'created')", (dumps(task_spec),)
            )
        db.close()
        return cls(path)

    def close(self) -> None:
        """Close the database connection."""
        self.db.close()

    def metadata(self) -> dict:
        """Decode the stored task specification, lifecycle status, and optional result."""
        row = self.db.execute("SELECT * FROM meta").fetchone()
        return {
            "spec": json.loads(row["spec"]),
            "status": row["status"],
            "pid": row["pid"],
            "reason": row["reason"],
            "result": _optional_json(row["result"]),
        }

    def record_status(self, status: str, pid: int | None = None, reason: str | None = None) -> None:
        """Persist status, process identity, and the optional explanation together."""
        with self.db:
            self.db.execute(
                "UPDATE meta SET status = ?, pid = ?, reason = ?", (status, pid, reason)
            )

    def record_result(self, status: str, result: dict) -> None:
        """Save the final report and clear the active process and pause explanation."""
        with self.db:
            self.db.execute(
                "UPDATE meta SET status = ?, pid = NULL, reason = NULL, result = ?",
                (status, dumps(result)),
            )

    def record_checkpoint(self, state: dict, tree: str, action: dict | None = None) -> int | None:
        """Atomically store state and an optional uncertain action, returning its identifier."""
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO checkpoints (time, turn, tree, state) VALUES (?, ?, ?, ?)",
                (now, state["turn"], tree, dumps(state)),
            )
            if action is None:
                return None
            cursor = self.db.execute(
                "INSERT INTO actions (time, turn, action, status) VALUES (?, ?, ?, 'uncertain')",
                (now, state["turn"], dumps(action)),
            )
            return cursor.lastrowid

    def complete_action(self, action_id: int, result: dict) -> None:
        """Attach an action result and mark that action done."""
        with self.db:
            self.db.execute(
                "UPDATE actions SET status = 'done', result = ? WHERE seq = ?",
                (dumps(result), action_id),
            )

    def last_checkpoint(self) -> dict | None:
        """Decode the most recently inserted checkpoint, or return None."""
        row = self.db.execute("SELECT * FROM checkpoints ORDER BY seq DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {"tree": row["tree"], "turn": row["turn"], "state": json.loads(row["state"])}

    def action_history(self, status: str | None = None) -> list[dict]:
        """Decode actions in insertion order, optionally selecting one status."""
        rows = self.db.execute("SELECT * FROM actions ORDER BY seq").fetchall()
        return [
            {
                "turn": row["turn"],
                "action": json.loads(row["action"]),
                "status": row["status"],
                "result": _optional_json(row["result"]),
            }
            for row in rows
            if status in (None, row["status"])
        ]

    def discard_uncertain(self) -> int:
        """Mark unresolved actions abandoned after explicit acceptance of the current state."""
        with self.db:
            return self.db.execute(
                "UPDATE actions SET status = 'abandoned' WHERE status = 'uncertain'"
            ).rowcount
