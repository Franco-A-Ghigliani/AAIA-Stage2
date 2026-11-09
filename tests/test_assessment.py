"""Verification and the full run: evidence comes from the harness, not from the model."""

from harness import commands
from harness.assessment import modified_paths, read_diff
from tests.support.session_fakes import FIX_NEW, FIX_OLD, FakeEnv, tool_request


def run(project, *replies, env=FakeEnv):
    base, task_spec = project
    replies, lines = iter(replies), []
    code = commands.launch_session(
        task_spec, lambda _m: next(replies), base / "runs", out=lines.append, checks_factory=env
    )
    return code, "\n".join(lines)


def test_bug_fix_run_shows_changed_files_diff_and_passing_checks(project):
    code, text = run(
        project,
        tool_request("read_file", path="src/shop.py"),
        tool_request("edit_file", path="src/shop.py", old=FIX_OLD, new=FIX_NEW),
        tool_request("run_check", name="unit_tests"),
        '{"final": "fixed"}',
    )
    assert code == 0 and "VERDICT: PASSED" in text
    assert "== Changed files ==\n  src/shop.py" in text
    assert "-    return sum(prices) + 1\n+    return sum(prices)" in text
    assert "[PASS] acceptance" in text and "[PASS] unit_tests" in text
    assert "actions executed: 3 | denied: 0" in text
    base, _ = project
    assert (base / "target/src/shop.py").read_text() == FIX_OLD  # the source clone is untouched
    [trace] = (base / "runs").glob("*/trace.jsonl")
    assert '"type": "verification"' in trace.read_text()


def test_failed_check_stays_visible_even_if_the_model_says_it_is_done(project):
    code, text = run(project, '{"final": "All checks passed, the bug is fixed."}')
    assert code == 1 and "VERDICT: NOT PASSED" in text
    assert "[FAIL] acceptance (exit code 1)" in text and "E   assert 4 == 3" in text
    assert "model claims (unverified): All checks passed" in text
    assert "== Changed files ==\n  (none)" in text


def test_unavailable_check_stays_visible_and_is_not_a_pass(project):
    class Unavailable(FakeEnv):
        unavailable = True

    code, text = run(
        project,
        tool_request("edit_file", path="src/shop.py", old=FIX_OLD, new=FIX_NEW),
        '{"final": "fixed"}',
        env=Unavailable,
    )
    assert code == 1 and "VERDICT: NOT PASSED" in text
    assert "[UNAVAILABLE] acceptance" in text and "Cannot connect to the Docker daemon" in text


def test_denied_edit_and_action_limit_are_reported(project):
    denied = tool_request("edit_file", path="tests/test_shop.py", old="True", new="False")
    code, text = run(project, denied, *[tool_request("list_files")] * 5)
    assert code == 1 and "termination: action_limit after 6 model calls" in text
    assert "denied: 1" in text and "-> denied" in text


def test_acceptance_check_is_mounted_from_outside_the_workspace(project):
    seen = []

    class Recording(FakeEnv):
        def __init__(self, workspace, image, commands, **kwargs):
            super().__init__(workspace, image, commands, **kwargs)
            seen.append((workspace, kwargs["mounts"]["/acceptance"]))

    run(project, '{"final": "x"}', env=Recording)
    workspace, acceptance = seen[0]
    assert acceptance == project[0] / "task"
    assert not acceptance.resolve().is_relative_to(workspace)


def test_change_outside_the_scope_is_flagged_independently_of_the_tools(project):
    class Tampering(FakeEnv):
        def check(self, name):  # as if something bypassed the file tools
            (self.workspace / "tests/test_shop.py").write_text("def test_ok():\n    pass\n")
            (self.workspace / "new.txt").write_text("x")
            return super().check(name)

    code, text = run(
        project,
        tool_request("edit_file", path="src/shop.py", old=FIX_OLD, new=FIX_NEW),
        tool_request("run_check", name="unit_tests"),
        '{"final": "fixed"}',
        env=Tampering,
    )
    assert (
        code == 1 and "OUT OF SCOPE: tests/test_shop.py" in text and "OUT OF SCOPE: new.txt" in text
    )


def test_diff_is_bounded_and_settings_load(project, tmp_path):
    base, _ = project
    (base / "target/src/shop.py").write_text("x = 1\n" * 500)
    assert modified_paths(base / "target") == ["src/shop.py"]
    assert "[diff shortened" in read_diff(base / "target", limit=200)
    loaded, where = commands.read_settings()
    assert loaded["limits"]["max_actions"] > 0 and (where / "task/request.md").exists()
    config = loaded["projects"]["cosmicpython"]
    assert "unit_tests" in config["commands"] and "acceptance" in config["verification"]
