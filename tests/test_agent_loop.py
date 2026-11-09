"""Controller: scripted model replies, so no live model or account is needed."""

from harness.agent_loop import TaskAgent
from tests.support.agent_fakes import (
    FakeEnv,
    MemoryRepository,
    observation_at,
    reply_sequence,
    tool_request,
)


def test_scripted_reply_calls_the_correct_tool_and_receives_its_result():
    repository, model = (
        MemoryRepository(),
        reply_sequence(tool_request("read_file", path="src/model.py"), '{"final": "done"}'),
    )
    completion = TaskAgent(model, repository, FakeEnv()).perform_task("fix it")
    assert repository.calls == [("read_file", "src/model.py")]
    observation = observation_at(model, 1)
    assert observation["result"] == {"status": "ok", "output": "file content"}
    assert observation["tool"] == "read_file" and observation["task_reminder"] == "fix it"
    assert model.seen[1][-1]["role"] == "user"  # results are data, never system instructions
    assert completion["termination"] == "final" and completion["final"] == "done"
    assert completion["counts"]["executed"] == 1


def test_unknown_tool_or_invalid_arguments_give_a_clear_error_without_executing():
    repository, env = MemoryRepository(), FakeEnv()
    model = reply_sequence(
        tool_request("bash", command="rm -rf /"),
        tool_request("read_file", file="x"),
        tool_request("read_file", path=3),
        '{"tool": "read_file", "args": "src/model.py"}',
        tool_request("run_check", name="deploy"),
        '{"final": "gave up"}',
    )
    completion = TaskAgent(model, repository, env).perform_task("fix it")
    assert repository.calls == [] and env.calls == []
    assert "unknown tool: 'bash'" in observation_at(model, 1)["result"]["output"]
    assert "takes exactly these arguments: ['path']" in observation_at(model, 2)["result"]["output"]
    assert "must be a string" in observation_at(model, 3)["result"]["output"]
    assert "unknown check: 'deploy'" in observation_at(model, 5)["result"]["output"]
    assert completion["counts"]["errors"] == 5


def test_invalid_json_is_counted_and_returned_as_an_error():
    model = reply_sequence("not JSON", "[1]", '{"final": 3}', '{"final": "ok"}')
    completion = TaskAgent(model, MemoryRepository(), FakeEnv()).perform_task("x")
    assert "invalid JSON" in observation_at(model, 1)["error"]
    assert completion["counts"]["invalid_replies"] == 3 and completion["turns"] == 4


def test_repeating_reply_reaches_the_action_limit_and_stops_further_actions():
    repository, calls = MemoryRepository(), []

    def model(messages):
        calls.append(1)
        return tool_request("list_files")

    completion = TaskAgent(model, repository, FakeEnv(), max_actions=4).perform_task("loop")
    assert completion["termination"] == "action_limit"
    assert len(calls) == 4 and len(repository.calls) == 4
    assert "after 4 actions" in completion["reason"]


def test_denied_actions_and_retries_are_counted_and_not_executed_again():
    repository = MemoryRepository()
    denied = tool_request("edit_file", path="tests/test_x.py", old="a", new="b")
    missing = tool_request("edit_file", path="src/model.py", old="missing", new="b")
    good = tool_request("edit_file", path="src/model.py", old="a", new="b")
    model = reply_sequence(denied, denied, missing, missing, good, missing, '{"final": "done"}')
    completion = TaskAgent(model, repository, FakeEnv()).perform_task("fix")
    # A repeat of a failed request is not run; after a successful edit it may run again.
    assert [c[1] for c in repository.calls] == [
        "tests/test_x.py",
        "src/model.py",
        "src/model.py",
        "src/model.py",
    ]
    assert observation_at(model, 1)["result"]["status"] == "denied"
    assert "repeated request" in observation_at(model, 2)["result"]["output"]["error"]
    counts = completion["counts"]
    assert (counts["denied"], counts["retries"], counts["errors"]) == (1, 2, 2)


def test_failed_check_is_returned_to_the_model_as_a_failure_with_its_output():
    env = FakeEnv(status="failed")
    model = reply_sequence(
        tool_request("run_check", name="unit_tests"), '{"final": "all checks passed"}'
    )
    TaskAgent(model, MemoryRepository(), env).perform_task("fix")
    result = observation_at(model, 1)["result"]
    assert result["status"] == "error"
    assert result["output"]["exit_code"] == 1 and result["output"]["output"] == "1 failed"


def test_model_error_and_cancellation_stop_safely():
    def broken(messages):
        raise TimeoutError("no answer")

    completion = TaskAgent(broken, MemoryRepository(), FakeEnv()).perform_task("x")
    assert completion["termination"] == "model_error" and "TimeoutError" in completion["reason"]

    def interrupted(messages):
        raise KeyboardInterrupt

    env = FakeEnv()
    completion = TaskAgent(interrupted, MemoryRepository(), env).perform_task("x")
    assert completion["termination"] == "cancelled" and env.stopped


def test_events_are_emitted_and_tools_are_described():
    events = []
    model = reply_sequence(tool_request("list_files"), '{"final": "done"}')
    controller = TaskAgent(model, MemoryRepository(), FakeEnv(), emit=events.append)
    controller.perform_task("x")
    assert [e["type"] for e in events] == ["request", "result", "final", "stop"]
    assert "unit_tests" in model.seen[0][0]["content"]
    assert [t["name"] for t in controller.tool_catalog()] == [
        "list_files",
        "read_file",
        "search",
        "find_symbol",
        "dependencies",
        "git_state",
        "edit_file",
        "run_check",
    ]


def test_fenced_json_reply_is_accepted():
    model = reply_sequence('```json\n{"final": "done"}\n```')
    completion = TaskAgent(model, MemoryRepository(), FakeEnv()).perform_task("x")
    assert completion["termination"] == "final" and completion["counts"]["invalid_replies"] == 0


def test_failed_check_guides_the_next_edit_and_a_passing_check_clears_the_count():
    class Flaky(FakeEnv):
        def check(self, name):
            self.status = "passed" if self.calls else "failed"
            return super().check(name)

    env = Flaky()
    model = reply_sequence(
        tool_request("run_check", name="unit_tests"),
        tool_request("edit_file", path="src/model.py", old="a", new="b"),
        tool_request("run_check", name="unit_tests"),
        '{"final": "fixed"}',
    )
    completion = TaskAgent(model, MemoryRepository(), env, max_failed_checks=2).perform_task("fix")
    assert observation_at(model, 1)["result"]["output"]["output"] == "1 failed"
    assert observation_at(model, 3)["result"]["status"] == "ok"
    assert completion["termination"] == "final"


def test_consecutive_failed_checks_stop_the_run_with_a_clear_reason():
    edits = [tool_request("edit_file", path="src/model.py", old=f"{i}", new="b") for i in range(3)]
    check = tool_request("run_check", name="unit_tests")
    model = reply_sequence(check, edits[0], check, edits[1], check)
    controller = TaskAgent(model, MemoryRepository(), FakeEnv("failed"), max_failed_checks=3)
    completion = controller.perform_task("fix")
    assert completion["termination"] == "retry_limit" and "3 failed checks" in completion["reason"]


def test_arguments_beside_the_tool_name_are_accepted_and_still_validated():
    repository = MemoryRepository()
    replies = [
        '{"tool": "read_file", "path": "a.py"}',
        '{"tool": "read_file", "nope": "a.py"}',
        '{"final": "done"}',
    ]
    completion = TaskAgent(lambda _m: replies.pop(0), repository, FakeEnv()).perform_task("task")
    assert repository.calls == [("read_file", "a.py")]
    assert completion["counts"]["invalid_replies"] == 0 and completion["counts"]["errors"] == 1


def test_a_run_of_invalid_replies_stops_early_and_a_valid_one_resets_it():
    calls = []

    def model(_messages):
        calls.append(1)
        return '{"tool": "list_files", "args": {}}' if len(calls) == 4 else "not json"

    completion = TaskAgent(model, MemoryRepository(), FakeEnv(), max_actions=40).perform_task(
        "task"
    )
    assert completion["termination"] == "retry_limit" and "5 invalid" in completion["reason"]
    assert completion["turns"] == 9  # 3 invalid, 1 valid, then 5 invalid in a row


def test_first_final_is_sent_back_for_review_and_the_review_can_lead_to_a_fix():
    repository = MemoryRepository()
    replies = [
        '{"final": "done"}',
        '{"tool": "read_file", "args": {"path": "a.py"}}',
        '{"final": "done, reviewed"}',
    ]
    seen = []

    def model(messages):
        seen.append(messages[-1]["content"])
        return replies.pop(0)

    completion = TaskAgent(model, repository, FakeEnv(), self_reviews=1).perform_task("task")
    assert "each requirement" in seen[1]
    assert repository.calls == [("read_file", "a.py")]
    assert completion["final"] == "done, reviewed" and completion["turns"] == 3
