import json
import tempfile
import unittest
import contextlib
import io
from pathlib import Path
from unittest import mock

from coding_harness.results import export_results, snapshot_files


class ResultsTest(unittest.TestCase):
    def test_cli_exports_before_runtime_cleanup_on_turn_limit(self):
        from coding_harness import cli, controller
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp)
            repository = package / 'repository'
            repository.mkdir()
            workspace = package / 'workspace'
            workspace.mkdir()
            (workspace / 'a.py').write_text('value = 1\n')

            class FakeRuntime:
                root = workspace

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    (workspace / 'a.py').unlink()
                    workspace.rmdir()

            def run(*args, **kwargs):
                (workspace / 'a.py').write_text('value = 2\n')
                return {'termination': 'turn_limit', 'reason': 'limit reached'}

            output = io.StringIO()
            git = {'root': str(repository), 'commit': 'abc123', 'branch': 'main',
                   'tracked_changes': [], 'clean': True}
            with mock.patch.object(controller, 'git_state', return_value=git), \
                    mock.patch.object(controller, 'Runtime', return_value=FakeRuntime()), \
                    mock.patch.object(controller, 'OllamaModel'), \
                    mock.patch.object(controller, 'run_agent', side_effect=run), \
                    mock.patch('sys.argv', ['harness', '--root', str(repository), '--task', 'fix',
                                            '--session-dir', str(package / 'sessions')]), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(), 1)
            artifacts = next((package / 'sessions').glob('*/results/run-001'))
            self.assertFalse(workspace.exists())
            self.assertEqual((artifacts / 'files/a.py').read_text(), 'value = 2\n')
            self.assertIn('modified: a.py', output.getvalue())
            self.assertIn(str(artifacts / 'changes.diff'), output.getvalue())

    def test_changes_survive_workspace_cleanup(self):
        with tempfile.TemporaryDirectory() as output:
            destination = Path(output) / 'results'
            with tempfile.TemporaryDirectory() as workspace:
                root = Path(workspace)
                (root / 'modified.py').write_bytes(b'value = 1\n')
                (root / 'deleted.txt').write_bytes(b'remove me\n')
                (root / 'unchanged.txt').write_bytes(b'keep me\n')
                before = snapshot_files(root)
                (root / 'modified.py').write_bytes(b'value = 2\n')
                (root / 'deleted.txt').unlink()
                (root / 'added.txt').write_bytes(b'new file')
                changes = export_results(before, root, destination)
            self.assertFalse(root.exists())
            self.assertEqual(changes, [
                {'path': 'added.txt', 'status': 'added'},
                {'path': 'deleted.txt', 'status': 'deleted'},
                {'path': 'modified.py', 'status': 'modified'},
            ])
            self.assertEqual(json.loads((destination / 'changes.json').read_text()), changes)
            self.assertEqual((destination / 'files/modified.py').read_bytes(), b'value = 2\n')
            self.assertFalse((destination / 'files/deleted.txt').exists())
            self.assertFalse((destination / 'files/unchanged.txt').exists())
            diff = (destination / 'changes.diff').read_text()
            self.assertIn('-value = 1\n+value = 2\n', diff)
            self.assertIn('+++ /dev/null', diff)
            self.assertIn('\\ No newline at end of file', diff)

    def test_binary_changes_and_empty_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'workspace'
            root.mkdir()
            (root / 'image.bin').write_bytes(b'\0\xff')
            destination = Path(temp) / 'binary'
            export_results({}, root, destination)
            self.assertEqual((destination / 'files/image.bin').read_bytes(), b'\0\xff')
            self.assertIn('Binary files differ', (destination / 'changes.diff').read_text())
            empty = Path(temp) / 'empty'
            self.assertEqual(export_results(snapshot_files(root), root, empty), [])
            self.assertEqual((empty / 'changes.diff').read_text(), '')

    def test_snapshot_excludes_links_hidden_and_special_files(self):
        import os
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'visible').write_bytes(b'ok')
            (root / '.hidden').write_bytes(b'private')
            try:
                (root / 'link').symlink_to(root / 'visible')
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            os.mkfifo(root / 'pipe')
            self.assertEqual(snapshot_files(root), {'visible': b'ok'})
