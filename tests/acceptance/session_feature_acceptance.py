"""Protected acceptance check for the Stage 1 session-persistence feature demo.

Run in the harness sandbox from the target repository root:
    python /protected/0/session_feature_acceptance.py

The file is mounted read-only and is not copied into the model-writable target.
It intentionally fails on the Stage 1 baseline because sessions do not exist yet.
"""

import tempfile
import sys
from pathlib import Path

target_root = Path("/workspace")
if not target_root.is_dir():
    target_root = Path.cwd()
sys.path.insert(0, str(target_root))

from coding_harness.sessions import SessionStore, digest_tree


def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        repository = root / "repository"
        repository.mkdir()
        (repository / "source.py").write_text("value = 1\n", encoding="utf-8")
        store = SessionStore(root / "saved-sessions")
        manifest = store.create(
            repository=repository,
            task="Persist a task and reopen it after process restart",
            starting_commit="abc123",
            source_state={"root": str(repository), "commit": "abc123",
                          "branch": "main", "tracked_changes": [], "clean": True},
            allowed_changes=["coding_harness/**"],
            checks=["python -m unittest discover -s tests"],
            protected_checks=[],
            max_turns=7,
            context_budget_chars=32000,
        )
        session_dir = store.session_path(manifest["id"])
        workspace = store.workspace_path(manifest["id"])
        (workspace / "source.py").write_text("value = 2\n", encoding="utf-8")
        manifest["history"] = [
            {"role": "system", "content": "fixed system rules"},
            {"role": "user", "content": "the original task"},
            {"role": "assistant", "content": '{"tool":"edit_file"}'},
        ]
        manifest["consumed_turns"] = 3
        manifest["workspace_hash"] = digest_tree(workspace)
        manifest["status"] = "cancelled"
        store.save(manifest)
        store.append_event(manifest["id"], {"type": "check", "status": "failed"})

        reopened = SessionStore(root / "saved-sessions")
        loaded = reopened.load(manifest["id"])
        assert loaded["task"].startswith("Persist a task")
        assert loaded["consumed_turns"] == 3
        assert loaded["history"][-1]["content"] == '{"tool":"edit_file"}'
        assert loaded["workspace_hash"] == digest_tree(workspace)
        assert loaded["status"] == "cancelled"
        assert any(item["id"] == manifest["id"] for item in reopened.list_sessions())
        events = (session_dir / "events.jsonl").read_text(encoding="utf-8")
        assert '"status": "failed"' in events

    print("PASS: task, history, limits, working-copy checkpoint, status, and events survive reopening")


if __name__ == "__main__":
    main()
