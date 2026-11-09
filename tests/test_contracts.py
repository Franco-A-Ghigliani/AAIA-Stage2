"""Recorded contracts from the application before its module reorganization."""

import copy
import json
import sqlite3
from pathlib import Path

from harness import commands, display
from harness.agent_loop import TaskAgent
from harness.journal import SessionJournal
from harness.prompt_context import PromptBuilder
from tests.support.agent_fakes import FakeEnv, MemoryRepository, reply_sequence, tool_request
from tests.support.context_fakes import SYSTEM, TASK, MappedRepository, steps

FIXTURES = Path(__file__).parent / "fixtures"


class RecordingJournal:
    def __init__(self):
        self.records = []

    def record_checkpoint(self, state, action=None):
        self.records.append(copy.deepcopy(["checkpoint", state, action]))
        return len(self.records) if action is not None else None

    def finish(self, action_id, result):
        self.records.append(copy.deepcopy(["finish", action_id, result]))


def capture_contracts():
    scenarios = {
        "success": [tool_request("read_file", path="src/model.py"), '{"final":"done"}'],
        "invalid": ["not JSON"] * 5,
        "repeat": [tool_request("edit_file", path="tests/x.py", old="a", new="b")] * 2,
        "review": ['{"final":"done"}', tool_request("list_files"), '{"final":"reviewed"}'],
        "flat_args": ['{"tool":"read_file","path":"a.py"}', '{"final":"done"}'],
        "check_failure": [tool_request("run_check", name="unit_tests"), '{"final":"done"}'],
    }
    captured = {}
    for label, replies in scenarios.items():
        model = reply_sequence(*replies)
        journal, events = RecordingJournal(), []
        controller = TaskAgent(
            model,
            MemoryRepository(),
            FakeEnv("failed" if label == "check_failure" else "passed"),
            max_actions=len(replies),
            emit=lambda event, target=events: target.append(copy.deepcopy(event)),
            journal=journal,
            self_reviews=int(label == "review"),
        )
        completion = controller.perform_task("Fix the calculation.")
        captured[label] = {
            "outcome": completion,
            "messages": model.seen,
            "events": events,
            "journal": journal.records,
        }
    captured["context"] = [
        PromptBuilder(MappedRepository(), limit).compose_messages(
            [SYSTEM, TASK, *steps(8, size=3000)]
        )
        for limit in (1500, 4000, 20000)
    ]
    argument_parser = commands.argument_parser()
    captured["help"] = argument_parser.format_help()
    captured["subcommands"] = {
        name: command.format_help()
        for action in argument_parser._actions
        if hasattr(action, "choices") and isinstance(action.choices, dict)
        for name, command in action.choices.items()
    }
    lines = []
    report = {
        "changed_files": ["src/a.py"],
        "out_of_scope": [],
        "diff": "example diff\n",
        "checks": [{"check": "unit", "status": "failed", "exit_code": 1, "output": "failed\n"}],
        "unfinished": ["check unit: failed"],
        "passed": False,
    }
    display.render_report(captured["success"]["outcome"], report, lines.append)
    captured["display"] = lines
    captured["progress"] = [
        display.format_event(event)
        for scenario in scenarios
        for event in captured[scenario]["events"]
    ]
    return captured


def test_recorded_messages_events_checkpoints_and_cli_are_unchanged():
    expected = json.loads((FIXTURES / "contracts.json").read_text(encoding="utf-8"))
    assert capture_contracts() == expected


def test_database_created_before_refactor_can_be_reopened(tmp_path):
    path = tmp_path / "session.db"
    with sqlite3.connect(path) as connection:
        connection.executescript((FIXTURES / "session.sql").read_text(encoding="utf-8"))
    connection.close()
    store = SessionJournal(path)
    try:
        assert store.metadata()["spec"] == {
            "request": "unchanged task",
            "limits": {"max_actions": 4},
        }
        assert store.last_checkpoint() == {
            "tree": "original-tree",
            "turn": 2,
            "state": {"turn": 2, "messages": [], "counts": {"model_calls": 2}},
        }
        assert store.action_history()[0]["status"] == "uncertain"
        store.discard_uncertain()
        assert store.action_history()[0]["status"] == "abandoned"
    finally:
        store.close()
