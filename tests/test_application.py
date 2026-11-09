"""Sessions: storage across restarts, cancellation, and safe resume."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness import application, commands, display
from harness.journal import DATABASE_NAME, SessionJournal
from tests.support.container_fakes import docker_stub
from tests.support.session_fakes import FIX_NEW, FIX_OLD, FakeEnv, tool_request

ROOT = Path(__file__).resolve().parents[1]


def start(project, *replies, env=FakeEnv):
    base, task_spec = project
    replies = iter(replies)
    result = application.begin_session(
        task_spec, lambda _m: next(replies), lambda _e: None, env, base / "runs"
    )
    return Path(result["run_dir"]), result


def resume(session_dir, task_spec, *replies, accept_state=False):
    replies = iter(replies)
    return application.continue_session(
        session_dir, task_spec, lambda _m: next(replies), lambda _e: None, FakeEnv, accept_state
    )


def edit():
    return tool_request("edit_file", path="src/shop.py", old=FIX_OLD, new=FIX_NEW)


def test_saved_details_steps_errors_and_checks_reopen_after_a_restart(project):
    denied = tool_request("edit_file", path="tests/test_shop.py", old="True", new="False")
    session_dir, _ = start(project, denied, '{"final": "done"}')
    info = application.inspect_session(
        session_dir
    )  # a new connection, as after restarting the application
    assert info["spec"]["request"] == "fix total()"
    assert info["spec"]["commit"] == project[1]["commit"]
    assert info["spec"]["writable"] == ["src/"] and "acceptance" in info["spec"]["verification"]
    assert [entry["result"]["status"] for entry in info["actions"]] == ["denied"]
    assert info["status"] == "failed"
    report = info["result"]["report"]
    assert {c["check"]: c["status"] for c in report["checks"]}["acceptance"] == "failed"
    assert "check acceptance: failed" in report["unfinished"]
    store = SessionJournal(session_dir / DATABASE_NAME)
    assert len(store.last_checkpoint()["state"]["messages"]) == 5
    store.close()
    [listed] = application.enumerate_sessions(project[0] / "runs")
    assert (listed["status"], listed["task"], listed["turn"]) == ("failed", "fix", 2)
    lines = []
    display.render_session(application.inspect_session(session_dir), lines.append)
    assert any("edit_file tests/test_shop.py -> denied" in line for line in lines)


def test_resume_continues_with_consumed_limits_and_the_harness_own_edits(project):
    base, task_spec = project
    replies = iter([edit()])

    def model(_messages):
        try:
            return next(replies)
        except StopIteration:
            raise TimeoutError("proxy down") from None

    result = application.begin_session(task_spec, model, lambda _e: None, FakeEnv, base / "runs")
    session_dir = Path(result["run_dir"])
    assert result["status"] == "stopped" and result["outcome"]["turns"] == 2
    seen = []

    def resumed(messages):
        seen.append(json.loads(messages[-1]["content"]))
        return '{"final": "fixed"}'

    after = application.continue_session(session_dir, task_spec, resumed, lambda _e: None, FakeEnv)
    assert after["status"] == "completed" and after["code"] == 0
    assert after["outcome"]["turns"] == 3 and after["outcome"]["counts"]["model_calls"] == 3
    assert "harness_note" in seen[0]
    assert "src/shop.py" in after["report"]["changed_files"]


def test_resume_cannot_exceed_the_action_limit(project):
    session_dir, first = start(project, *[tool_request("list_files")] * 6)
    assert first["outcome"]["termination"] == "action_limit"
    calls = []
    after = application.continue_session(
        session_dir, project[1], lambda _m: calls.append(1), lambda _e: None, FakeEnv
    )
    assert after["outcome"]["termination"] == "action_limit" and calls == []


def test_changed_working_copy_pauses_until_a_person_accepts_it(project):
    session_dir, _ = start(project, edit(), '{"final": "x"}')
    meta_store = SessionJournal(session_dir / DATABASE_NAME)
    meta_store.record_status("stopped")
    meta_store.close()
    (session_dir / "workspace/src/shop.py").write_text("changed by someone\n")
    paused = resume(session_dir, project[1], '{"final": "never used"}')
    assert paused["status"] == "paused" and paused["code"] == 3
    assert "differs from the last checkpoint" in paused["problems"][0]
    assert application.inspect_session(session_dir)["status"] == "paused"
    assert (
        resume(session_dir, project[1], '{"final": "ok"}', accept_state=True)["status"] == "failed"
    )


def test_permission_change_blocks_resume_even_when_accepted(project):
    session_dir, _ = start(project, '{"final": "x"}')
    narrower = {**project[1], "writable": ["src/other/"]}
    paused = resume(session_dir, narrower, accept_state=True)
    assert paused["status"] == "paused" and "writable changed" in paused["problems"][0]
    assert "no longer allow" in resume(session_dir, None, accept_state=True)["problems"][0]


def test_completed_session_is_not_resumed(project):
    session_dir, first = start(project, edit(), '{"final": "fixed"}')
    assert first["status"] == "completed"
    assert "already completed" in resume(session_dir, project[1], accept_state=True)["problems"][0]
    assert application.inspect_session(session_dir)["status"] == "completed"


def test_uncertain_action_pauses_and_is_never_replayed(project, monkeypatch):
    monkeypatch.setattr(application, "_process_exists", lambda _pid: False)
    session_dir, _ = start(project, '{"final": "x"}')
    store = SessionJournal(session_dir / DATABASE_NAME)
    latest = store.last_checkpoint()
    store.record_checkpoint(
        latest["state"], latest["tree"], json.loads(edit())
    )  # logged, never finished
    store.record_status("running", pid=999999)
    store.close()
    assert application.inspect_session(session_dir)["status"] == "interrupted"
    paused = resume(session_dir, project[1], '{"final": "never used"}')
    assert paused["status"] == "paused" and "uncertain interrupted action" in paused["problems"][0]
    assert (session_dir / "workspace/src/shop.py").read_text() == FIX_OLD
    after = resume(session_dir, project[1], '{"final": "ok"}', accept_state=True)
    assert after["status"] == "failed"
    assert (
        session_dir / "workspace/src/shop.py"
    ).read_text() == FIX_OLD  # the edit was not repeated
    assert [e["status"] for e in application.inspect_session(session_dir)["actions"]] == [
        "abandoned"
    ]


def test_running_session_cannot_be_resumed_twice(project, monkeypatch):
    monkeypatch.setattr(application, "_process_exists", lambda pid: pid == os.getpid())
    session_dir, _ = start(project, '{"final": "x"}')
    store = SessionJournal(session_dir / DATABASE_NAME)
    store.record_status("running", pid=os.getpid())
    store.close()
    assert "still running" in resume(session_dir, project[1], accept_state=True)["problems"][0]


def test_unknown_project_or_task_is_refused():
    settings, base = commands.read_settings()
    assert application.prepare_task(settings, base, "cosmicpython", "bugfix")["writable"]
    with pytest.raises(RuntimeError, match="not allowed"):
        application.prepare_task(settings, base, "other", "bugfix")
    with pytest.raises(RuntimeError, match="unknown task"):
        application.prepare_task(settings, base, "cosmicpython", "other")


def test_session_argument_cannot_leave_the_runs_directory(tmp_path):
    with pytest.raises(RuntimeError, match="unknown session"):
        commands._locate_session(tmp_path, "../secret")


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX signals, executable scripts, and ps")
def test_cancelling_a_running_check_stops_its_children_and_further_writes(project, tmp_path):
    base, task_spec = project
    late, pids = tmp_path / "late-write.txt", tmp_path / "child-pid.txt"
    child = f"import time; time.sleep(3); open({str(late)!r}, 'w').write('x')"
    docker = docker_stub(
        tmp_path,
        "import subprocess, time\n"
        "if sys.argv[1] == 'kill':\n    sys.exit(0)\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        f"open({str(pids)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(30)",
    )
    script = tmp_path / "runner.py"
    script.write_text(
        "import json, signal, sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from functools import partial\nfrom pathlib import Path\n"
        "from harness import commands, application\n"
        "from harness.containers import ContainerChecks\n"
        "signal.signal(signal.SIGINT, commands._interrupt)\n"
        f"task_spec = json.loads({json.dumps(json.dumps(task_spec))})\n"
        "replies = iter([json.dumps({'tool': 'run_check', 'args': {'name': 'unit_tests'}})])\n"
        f"env = partial(ContainerChecks, docker={docker!r})\n"
        "model = lambda _m: next(replies)\n"
        f"application.begin_session(task_spec, model, lambda _e: None, env, Path({str(base / 'runs')!r}))\n"
    )
    process = subprocess.Popen([sys.executable, str(script)])
    try:
        deadline = time.monotonic() + 20
        while not pids.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pids.exists(), "the check did not start"
        [db] = (base / "runs").glob(f"*/{DATABASE_NAME}")
        assert application.request_cancellation(db.parent) is True
        process.wait(timeout=15)
    finally:
        process.kill()
    info = application.inspect_session(db.parent)
    assert info["status"] == "cancelled"
    assert info["result"]["outcome"]["termination"] == "cancelled"
    assert [e["status"] for e in info["actions"]] == ["uncertain"]
    assert "verification checks were not run" in info["result"]["report"]["unfinished"]
    assert (
        subprocess.run(["ps", "-p", pids.read_text()], capture_output=True, check=False).returncode
        != 0
    )
    time.sleep(3.2)
    assert not late.exists()  # no write was left behind
    assert application.request_cancellation(db.parent) is False
