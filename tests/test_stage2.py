import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.agent import run_agent
from coding_harness.context import ContextBudgetError, ContextManager
from coding_harness.navigation import (discover_dependencies, find_python_symbols,
                                       git_state, repository_map)
from coding_harness.runtime import Runtime
from coding_harness.runtime import _bounded_result
from coding_harness.sessions import SessionError, SessionStore, digest_tree


class NavigationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "module.py").write_text(
            "class SessionStore:\n    def save_checkpoint(self):\n        pass\n",
            encoding="utf-8",
        )
        (self.root / "requirements.txt").write_text("requests>=2\n# comment\n", encoding="utf-8")
        (self.root / "pyproject.toml").write_text(
            '[project]\ndependencies = ["tomli>=2"]\n', encoding="utf-8")

    def test_map_symbols_and_dependencies(self):
        summary = repository_map(self.root)
        self.assertIn("pkg/module.py", summary["files"])
        self.assertIn(".py", summary["languages"])
        symbols = find_python_symbols(self.root, "save")
        self.assertEqual(symbols[0]["name"], "save_checkpoint")
        dependencies = discover_dependencies(self.root)
        self.assertIn("requests>=2", dependencies["dependencies"])
        self.assertIn("tomli>=2", dependencies["dependencies"])

    def test_git_state_reports_exact_commit_and_tracked_changes(self):
        subprocess.run(["git", "init", str(self.root)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.root), "add", "pkg/module.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-m", "base"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        initial = git_state(self.root)
        self.assertEqual(len(initial["commit"]), 40)
        self.assertTrue(initial["clean"])
        (self.root / "pkg" / "module.py").write_text("changed\n", encoding="utf-8")
        self.assertFalse(git_state(self.root)["clean"])


class ContextTest(unittest.TestCase):
    def test_preserves_task_and_recent_history_within_budget(self):
        manager = ContextManager(2_000)
        messages = [
            {"role": "system", "content": "system rules"},
            {"role": "user", "content": "acceptance criteria must remain"},
            *[{"role": "assistant", "content": f"old-{i}-" + "x" * 700}
              for i in range(8)],
            {"role": "user", "content": "recent observation"},
        ]
        fitted = manager.fit(messages)
        self.assertLessEqual(sum(len(item["content"]) for item in fitted), 2_000)
        self.assertEqual(fitted[0]["content"], "system rules")
        self.assertEqual(fitted[1]["content"], "acceptance criteria must remain")
        self.assertEqual(fitted[-1]["content"], "recent observation")
        self.assertIn("untrusted data", " ".join(x["content"] for x in fitted).lower())

    def test_refuses_to_truncate_system_rules_or_task(self):
        manager = ContextManager(2_000)
        with self.assertRaises(ContextBudgetError):
            manager.fit([{"role": "system", "content": "x" * 1500},
                         {"role": "user", "content": "y" * 600}])


class SessionStoreTest(unittest.TestCase):
    def make_session(self, root):
        repo = root / "repo"
        repo.mkdir()
        (repo / "a.py").write_text("value = 1\n", encoding="utf-8")
        store = SessionStore(root / "sessions")
        manifest = store.create(
            repository=repo, task="persist this", starting_commit="abc",
            source_state={"root": str(repo), "commit": "abc", "tracked_changes": []},
            allowed_changes=["pkg/**"], checks=["python -m unittest"],
            protected_checks=[], max_turns=9, context_budget_chars=32000)
        workspace = store._directory(manifest["id"]) / "workspace"
        (workspace / "a.py").write_text("value = 2\n", encoding="utf-8")
        manifest.update({"history": [{"role": "user", "content": "persist this"}],
                         "consumed_turns": 4, "workspace_hash": digest_tree(workspace),
                         "status": "cancelled"})
        store.save(manifest)
        store.append_event(manifest["id"], {"type": "check", "status": "failed"})
        return repo, store, manifest

    def test_reopens_task_history_limits_events_and_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, store, original = self.make_session(root)
            reopened = SessionStore(root / "sessions")
            loaded = reopened.load(original["id"])
            workspace = reopened._directory(loaded["id"]) / "workspace"
            self.assertEqual(loaded["repository"], str(repo))
            self.assertEqual(loaded["history"][-1]["content"], "persist this")
            self.assertEqual(loaded["consumed_turns"], 4)
            self.assertEqual(loaded["workspace_hash"], digest_tree(workspace))
            self.assertIn("failed", (reopened._directory(loaded["id"]) / "events.jsonl").read_text())

    def test_digest_detects_manual_workspace_change(self):
        with tempfile.TemporaryDirectory() as temp:
            _, store, manifest = self.make_session(Path(temp))
            workspace = store._directory(manifest["id"]) / "workspace"
            expected = manifest["workspace_hash"]
            (workspace / "a.py").write_text("manual change\n", encoding="utf-8")
            self.assertNotEqual(expected, digest_tree(workspace))

    def test_concurrent_session_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            _, store, manifest = self.make_session(Path(temp))
            lock = store.acquire(manifest["id"])
            try:
                with self.assertRaises(SessionError):
                    store.acquire(manifest["id"])
            finally:
                store.release(lock)


class RuntimeScopeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "a.py").write_text("value = 1\n", encoding="utf-8")
        with mock.patch("coding_harness.sandbox.subprocess.run", return_value=
                        subprocess.CompletedProcess([], 0, b"pkg/a.py\0")):
            self.runtime = Runtime(self.repo, allowed_changes=["pkg/*.py"])
        self.addCleanup(self.runtime.close)

    def test_navigation_tools_and_file_scope(self):
        self.assertIn("pkg/a.py", self.runtime.execute(
            {"tool": "repo_map", "args": {}})["output"]["files"])
        self.assertEqual(self.runtime.execute(
            {"tool": "find_symbols", "args": {"query": "missing"}})["status"], "ok")
        denied = self.runtime.execute(
            {"tool": "write_file", "args": {"path": "other/a.py", "content": "x"}})
        self.assertEqual(denied["status"], "denied")
        bash = self.runtime.execute({"tool": "bash", "args": {"command": "true"}})
        self.assertEqual(bash["status"], "denied")

    def test_check_uses_read_only_workspace_and_protected_mounts(self):
        protected = self.repo / "protected"
        protected.mkdir()
        with mock.patch.object(self.runtime.sandbox, "run",
                               return_value={"exit_code": 0, "output": "PASS"}) as run:
            result = self.runtime.run_check("python /protected/0/check.py", [protected])
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(run.call_args.kwargs["readonly_workspace"])
        self.assertEqual(run.call_args.kwargs["readonly_mounts"], [protected.resolve()])

    def test_large_tool_outputs_are_bounded_and_marked(self):
        result = _bounded_result("error", "é" * 20_000)
        self.assertTrue(result["output"]["truncated"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode("utf-8")), 12_000)
        self.assertEqual(result["status"], "error")


class AgentCheckpointTest(unittest.TestCase):
    def test_model_cancellation_clears_pending_request_for_safe_resume(self):
        checkpoints = []

        def model(_messages):
            raise KeyboardInterrupt

        result = run_agent(model, type("Runtime", (), {"tools": []})(), "task",
                           checkpoint=lambda messages, turn, pending:
                           checkpoints.append((turn, pending)))
        self.assertEqual(result["termination"], "cancelled")
        self.assertEqual(checkpoints[0][1]["type"], "model_request")
        self.assertIsNone(checkpoints[-1][1])

    def test_failed_check_output_is_given_to_model_before_retry(self):
        from coding_harness import cli, controller
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
            store = SessionStore(root / "sessions")
            manifest = store.create(
                repository=repository, task="add behavior", starting_commit="abc",
                source_state={"root": str(repository), "commit": "abc", "tracked_changes": []},
                allowed_changes=["**"], checks=["python acceptance.py"],
                protected_checks=[], max_turns=4, context_budget_chars=32000)
            session_dir = store._directory(manifest["id"])
            workspace = session_dir / "workspace"
            (workspace / "app.py").write_text("value = 2\n", encoding="utf-8")
            baseline = session_dir / "baseline"
            baseline.mkdir()
            (baseline / "app.py").write_text("value = 1\n", encoding="utf-8")
            manifest["workspace_hash"] = digest_tree(workspace)

            class FakeRuntime:
                root = workspace
                approve = None

                def __init__(self):
                    self.checks = iter([
                        {"exit_code": 1, "output": "expected value 3"},
                        {"exit_code": 0, "output": "PASS"},
                    ])

                def run_check(self, command, protected):
                    return next(self.checks)

            attempts = []

            def fake_run_agent(_model, _runtime, task, *, max_turns, emit,
                               history, context_manager, checkpoint, turn_offset):
                attempts.append(list(history))
                updated = list(history) + [{"role": "assistant", "content": '{"final":"done"}'}]
                checkpoint(updated, turn_offset + 1, None)
                return {"termination": "final", "turns": 1, "final": "done"}

            orchestrator = controller.SessionController(
                store, model_factory=mock.Mock())
            with mock.patch.object(controller, "run_agent", side_effect=fake_run_agent):
                result = orchestrator._run_and_export(
                    manifest, FakeRuntime(),
                    [{"role": "system", "content": "rules"},
                     {"role": "user", "content": "task"}],
                    ContextManager(), baseline)
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(len(attempts), 2)
            self.assertIn("verification", attempts[1][-1]["content"])
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["check_history"][0]["results"][0]["status"], "failed")
            self.assertEqual(manifest["check_results"][0]["status"], "passed")


class ResumeSafetyTest(unittest.TestCase):
    def test_resume_requires_matching_source_and_workspace_and_clears_orphans_first(self):
        from coding_harness import controller
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            store = SessionStore(root / "sessions")
            state = {"root": str(repository), "commit": "abc", "tracked_changes": [],
                     "branch": "main", "clean": True}
            manifest = store.create(repository=repository, task="resume", starting_commit="abc",
                                    source_state=state, allowed_changes=["**"], checks=[],
                                    protected_checks=[], max_turns=5, context_budget_chars=32000)
            workspace = store._directory(manifest["id"]) / "workspace"
            (workspace / "a.py").write_text("baseline\n", encoding="utf-8")
            manifest["workspace_hash"] = digest_tree(workspace)
            manifest["status"] = "cancelled"
            store.save(manifest)
            orchestrator = controller.SessionController(store)
            with mock.patch.object(controller, "git_state", return_value=state), \
                    mock.patch.object(controller.DockerSandbox, "cleanup_session") as cleanup:
                loaded, _context, initialize = orchestrator.prepare_resume(manifest["id"])
                self.assertFalse(initialize)
                self.assertEqual(loaded["id"], manifest["id"])
                cleanup.assert_called_once_with(manifest["id"])
            (workspace / "a.py").write_text("external edit\n", encoding="utf-8")
            with mock.patch.object(controller, "git_state", return_value=state), \
                    mock.patch.object(controller.DockerSandbox, "cleanup_session"):
                with self.assertRaises(SessionError):
                    orchestrator.prepare_resume(manifest["id"])
            self.assertEqual(store.load(manifest["id"])["status"], "paused_for_review")


class CheckStatusTest(unittest.TestCase):
    def test_unavailable_check_is_stored_as_unavailable(self):
        from coding_harness.controller import SessionController
        from coding_harness.runtime import SandboxError
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            store = SessionStore(root / "sessions")
            manifest = store.create(
                repository=repository, task="check", starting_commit="abc",
                source_state={"root": str(repository), "commit": "abc", "tracked_changes": []},
                allowed_changes=["**"], checks=["python -m unittest"],
                protected_checks=[], max_turns=3, context_budget_chars=32000)
            workspace = store._directory(manifest["id"]) / "workspace"

            class RuntimeStub:
                root = workspace

                def run_check(self, command, mounts):
                    raise SandboxError("Docker unavailable")

            controller = SessionController(store)
            results = controller._run_checks(manifest, RuntimeStub(), 1)
            self.assertEqual(results[0]["status"], "unavailable")
            self.assertEqual(store.load(manifest["id"])["check_results"][0]["status"], "unavailable")

    def test_checks_are_unavailable_when_model_stops_before_verification(self):
        from coding_harness import controller
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            store = SessionStore(root / "sessions")
            manifest = store.create(
                repository=repository, task="incomplete", starting_commit="abc",
                source_state={"root": str(repository), "commit": "abc", "tracked_changes": []},
                allowed_changes=["**"], checks=["python -m unittest"],
                protected_checks=[], max_turns=1, context_budget_chars=32000)
            workspace = store.workspace_path(manifest["id"])
            (workspace / "a.py").write_text("value = 1\n", encoding="utf-8")
            baseline = store.session_path(manifest["id"]) / "baseline"
            baseline.mkdir()
            (baseline / "a.py").write_text("value = 1\n", encoding="utf-8")
            manifest["workspace_hash"] = digest_tree(workspace)

            class RuntimeStub:
                root = workspace
                approve = None

            orchestrator = controller.SessionController(store, model_factory=mock.Mock())
            with mock.patch.object(controller, "run_agent", return_value={
                    "termination": "turn_limit", "turns": 1, "reason": "limit"}):
                result = orchestrator._run_and_export(
                    manifest, RuntimeStub(),
                    [{"role": "system", "content": "rules"},
                     {"role": "user", "content": "task"}], ContextManager(), baseline)
            self.assertEqual(result["check_results"][0]["status"], "unavailable")
            self.assertIn("not run", result["check_results"][0]["reason"])

    def test_resume_mid_verification_runs_only_remaining_checks(self):
        from coding_harness import controller
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            store = SessionStore(root / "sessions")
            manifest = store.create(
                repository=repository, task="verify", starting_commit="abc",
                source_state={"root": str(repository), "commit": "abc", "tracked_changes": []},
                allowed_changes=["**"], checks=["check one", "check two"],
                protected_checks=[], max_turns=5, context_budget_chars=32000)
            workspace = store.workspace_path(manifest["id"])
            (workspace / "a.py").write_text("value = 1\n", encoding="utf-8")
            baseline = store.session_path(manifest["id"]) / "baseline"
            baseline.mkdir()
            (baseline / "a.py").write_text("value = 1\n", encoding="utf-8")
            manifest.update({
                "status": "checking",
                "current_check_cycle": 1,
                "outcome": {"termination": "final", "turns": 1, "final": "ready"},
                "consumed_turns": 1,
                "workspace_hash": digest_tree(workspace),
                "check_results": [{"command": "check one", "status": "passed",
                                   "exit_code": 0, "output": "PASS", "reason": None}],
            })
            store.save(manifest)

            class RuntimeStub:
                root = workspace
                approve = None

                def __init__(self):
                    self.commands = []

                def run_check(self, command, mounts):
                    self.commands.append(command)
                    return {"exit_code": 0, "output": "PASS"}

            runtime = RuntimeStub()
            orchestrator = controller.SessionController(store, model_factory=mock.Mock())
            with mock.patch.object(controller, "run_agent",
                                   side_effect=AssertionError("must not replay final model turn")):
                result = orchestrator._run_and_export(
                    manifest, runtime, [{"role": "system", "content": "rules"}],
                    ContextManager(), baseline)
            self.assertEqual(runtime.commands, ["check two"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(manifest["check_history"]), 1)

    def test_uncertain_action_is_never_replayed(self):
        from coding_harness import controller
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repo"
            repository.mkdir()
            store = SessionStore(root / "sessions")
            state = {"root": str(repository), "commit": "abc", "tracked_changes": [],
                     "branch": "main", "clean": True}
            manifest = store.create(repository=repository, task="resume", starting_commit="abc",
                                    source_state=state, allowed_changes=["**"], checks=[],
                                    protected_checks=[], max_turns=5, context_budget_chars=32000)
            manifest["status"] = "cancelled"
            manifest["pending_action"] = {"type": "tool_action", "action": {"tool": "write_file"}}
            store.save(manifest)
            orchestrator = controller.SessionController(store)
            with mock.patch.object(controller, "git_state", return_value=state), \
                    mock.patch.object(controller.DockerSandbox, "cleanup_session") as cleanup:
                with self.assertRaisesRegex(SessionError, "uncertain"):
                    orchestrator.prepare_resume(manifest["id"])
                cleanup.assert_called_once_with(manifest["id"])
            paused = store.load(manifest["id"])
            self.assertEqual(paused["status"], "paused_for_review")
            self.assertIsNotNone(paused["pending_action"])


if __name__ == "__main__":
    unittest.main()
