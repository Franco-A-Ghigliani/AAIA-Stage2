"""Reusable fixtures for behavior-focused tests."""

import json

from harness.errors import Denied, ToolError


class MemoryRepository:
    def __init__(self):
        self.calls = []

    def enumerate_files(self):
        self.calls.append(("list_files",))
        return ["src/model.py"]

    def read_text(self, path):
        self.calls.append(("read_file", path))
        return "file content"

    def search_text(self, query):
        self.calls.append(("search", query))
        return []

    def locate_symbol(self, name):
        self.calls.append(("find_symbol", name))
        return {"definitions": [], "references": []}

    def import_relationships(self, path):
        self.calls.append(("dependencies", path))
        return {"imports": [], "imported_by": []}

    def describe_changes(self):
        self.calls.append(("git_state",))
        return {"head": "abc", "changed_files": [], "diff_stat": ""}

    def replace_text(self, path, old, new):
        self.calls.append(("edit_file", path))
        if path.startswith("tests/"):
            raise Denied("tests/ is outside the writable area")
        if old == "missing":
            raise ToolError("old text occurs 0 times; it must occur exactly once")
        return f"edited {path}"


class FakeEnv:
    def __init__(self, status="passed"):
        self.commands = {"unit_tests": ["pytest"]}
        self.status, self.calls, self.stopped = status, [], False

    def check(self, name):
        if name not in self.commands:
            raise ToolError(f"unknown check: {name!r}")
        self.calls.append(name)
        return {
            "check": name,
            "status": self.status,
            "exit_code": int(self.status != "passed"),
            "output": "1 failed" if self.status != "passed" else "20 passed",
            "shortened": False,
        }

    def stop(self):
        self.stopped = True


def reply_sequence(*replies):
    """A model that returns the given replies in order and records what it was sent."""
    replies, seen = iter(replies), []

    def model(messages):
        seen.append([dict(m) for m in messages])
        return next(replies)

    model.seen = seen
    return model


def tool_request(tool, **args):
    return json.dumps({"tool": tool, "args": args})


def observation_at(model, call):
    return json.loads(model.seen[call][-1]["content"])
