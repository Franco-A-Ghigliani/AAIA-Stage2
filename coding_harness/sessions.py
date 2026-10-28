"""Persistent session metadata, event history, and working-copy fingerprints."""

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path


SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
SCHEMA_VERSION = 1


class SessionError(RuntimeError):
    pass


def default_session_dir():
    return Path.home() / ".coding-harness" / "sessions"


def digest_tree(root):
    """Hash visible regular files and their relative names in stable order."""
    root = Path(root)
    digest = hashlib.sha256()
    files = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames
                             if not (Path(directory) / name).is_symlink()
                             and not (Path(directory) / name).is_junction())
        for name in filenames:
            path = Path(directory) / name
            if path.is_symlink() or path.is_junction():
                continue
            if path.is_file():
                files.append(path)
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


class SessionStore:
    """Store one manifest and append-only event journal per task."""

    def __init__(self, root=None):
        requested = Path(root or default_session_dir()).expanduser()
        if requested.exists() and (requested.is_symlink() or requested.is_junction()):
            raise SessionError("session directory cannot be a link")
        self.root = requested.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.is_junction():
            raise SessionError("session directory cannot be a link")

    def _directory(self, session_id):
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise SessionError("invalid session id")
        path = self.root / session_id
        if path.is_symlink() or path.is_junction():
            raise SessionError("session directory cannot be a link")
        try:
            path.resolve().relative_to(self.root)
        except ValueError:
            raise SessionError("session path is outside the session store") from None
        return path

    def session_path(self, session_id):
        """Return a validated session directory for review tooling."""
        return self._directory(session_id)

    def workspace_path(self, session_id):
        """Return the persistent disposable workspace for a session."""
        return self._directory(session_id) / "workspace"

    def create(self, *, repository, task, starting_commit, source_state,
               allowed_changes, checks, protected_checks, max_turns,
               context_budget_chars):
        session_id = uuid.uuid4().hex
        directory = self._directory(session_id)
        directory.mkdir(mode=0o700)
        (directory / "workspace").mkdir()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "id": session_id,
            "repository": str(Path(repository).resolve()),
            "task": task,
            "starting_commit": starting_commit,
            "source_state": source_state,
            "allowed_changes": allowed_changes or ["**"],
            "checks": checks,
            "protected_checks": protected_checks,
            "max_turns": max_turns,
            "consumed_turns": 0,
            "context_budget_chars": context_budget_chars,
            "history": [],
            "pending_action": None,
            "workspace_hash": None,
            "status": "created",
            "check_results": [],
            "check_history": [],
            "current_check_cycle": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self.save(manifest)
        self.append_event(session_id, {"type": "created", "commit": starting_commit})
        return manifest

    def load(self, session_id):
        directory = self._directory(session_id)
        manifest_path = directory / "session.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise SessionError(f"session {session_id} does not exist")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SessionError(f"cannot read session {session_id}: {exc}") from None
        if not isinstance(manifest, dict):
            raise SessionError("session manifest must contain a JSON object")
        if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("id") != session_id:
            raise SessionError("unsupported or inconsistent session manifest")
        for key in ("repository", "task", "starting_commit", "status"):
            if not isinstance(manifest.get(key), str):
                raise SessionError(f"invalid session field: {key}")
        for key in ("allowed_changes", "checks", "protected_checks", "history", "check_results"):
            if not isinstance(manifest.get(key), list):
                raise SessionError(f"invalid session field: {key}")
        if (not all(isinstance(value, str) for value in manifest["allowed_changes"])
                or not all(isinstance(value, str) for value in manifest["checks"])
                or not all(isinstance(value, str) for value in manifest["protected_checks"])
                or not all(isinstance(value, dict) for value in manifest["history"])):
            raise SessionError("session permissions, checks, or history have an invalid shape")
        if (not isinstance(manifest.get("max_turns"), int)
                or not isinstance(manifest.get("consumed_turns"), int)
                or not 1 <= manifest["max_turns"] <= 100
                or manifest["consumed_turns"] < 0
                or manifest["consumed_turns"] > manifest["max_turns"]):
            raise SessionError("session call limits have an invalid shape")
        if (not isinstance(manifest.get("context_budget_chars"), int)
                or not 2_000 <= manifest["context_budget_chars"] <= 64_000):
            raise SessionError("session context budget has an invalid shape")
        source_state = manifest.get("source_state")
        if (not isinstance(source_state, dict)
                or source_state.get("root") != manifest["repository"]
                or source_state.get("commit") != manifest["starting_commit"]):
            raise SessionError("saved source identity is inconsistent")
        if manifest.get("pending_action") is not None and not isinstance(manifest["pending_action"], dict):
            raise SessionError("saved pending action has an invalid shape")
        current_cycle = manifest.get("current_check_cycle")
        if current_cycle is not None and (not isinstance(current_cycle, int) or current_cycle < 1):
            raise SessionError("saved check cycle has an invalid shape")
        if (not isinstance(manifest.get("check_history"), list)
                or not all(isinstance(item, dict) for item in manifest["check_history"])):
            raise SessionError("saved check history has an invalid shape")
        return manifest

    def save(self, manifest):
        directory = self._directory(manifest.get("id"))
        manifest["updated_at"] = time.time()
        temporary = directory / "session.json.tmp"
        destination = directory / "session.json"
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)

    def append_event(self, session_id, event):
        directory = self._directory(session_id)
        path = directory / "events.jsonl"
        payload = {"time": time.time(), **event}
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def list_sessions(self):
        sessions = []
        for path in sorted(self.root.iterdir(), key=lambda item: item.name):
            if not path.is_dir() or path.is_symlink() or not SESSION_ID.fullmatch(path.name):
                continue
            try:
                sessions.append(self.load(path.name))
            except SessionError:
                continue
        return sessions

    def acquire(self, session_id):
        """Acquire a process lock; remove it only when its owner is gone."""
        path = self._directory(session_id) / "session.lock"
        payload = json.dumps({"pid": os.getpid(), "time": time.time()})
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            return path
        except FileExistsError:
            try:
                owner = json.loads(path.read_text(encoding="utf-8")).get("pid")
                os.kill(int(owner), 0)
            except ProcessLookupError:
                try:
                    path.unlink()
                except OSError:
                    raise SessionError("session appears locked and its lock cannot be recovered") from None
                return self.acquire(session_id)
            except PermissionError:
                raise SessionError("session is already active in another process") from None
            except (TypeError, ValueError, json.JSONDecodeError):
                try:
                    path.unlink()
                except OSError:
                    raise SessionError("session appears locked and its lock cannot be recovered") from None
                return self.acquire(session_id)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 87:
                    try:
                        path.unlink()
                    except OSError:
                        raise SessionError("session appears locked and its lock cannot be recovered") from None
                    return self.acquire(session_id)
                raise SessionError("cannot verify the current session lock owner") from None
            raise SessionError("session is already active in another process")

    @staticmethod
    def release(lock_path):
        if lock_path:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
