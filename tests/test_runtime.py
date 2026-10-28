"""Security regression tests. Docker control is mocked; no host Bash runs."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from coding_harness.runtime import Runtime
from coding_harness.sandbox import DockerSandbox, SandboxError, snapshot_repository


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)
        (self.source / 'a.py').write_text('value = 1\n')
        (self.source / 'untracked.txt').write_text('private')
        patch = mock.patch('coding_harness.sandbox.subprocess.run', return_value=
                           subprocess.CompletedProcess([], 0, b'a.py\0'))
        patch.start()
        self.addCleanup(patch.stop)
        self.runtime = Runtime(self.source)
        self.addCleanup(self.runtime.close)

    def call(self, tool, **args):
        return self.runtime.execute({'tool': tool, 'args': args})

    def test_disposable_copy(self):
        self.assertEqual(self.call('edit_file', path='a.py', old='1', new='2')['status'], 'ok')
        self.assertEqual((self.source / 'a.py').read_text(), 'value = 1\n')
        self.assertEqual((self.runtime.root / 'a.py').read_text(), 'value = 2\n')
        self.assertFalse((self.runtime.root / 'untracked.txt').exists())
        root = self.runtime.root
        self.runtime.close()
        self.assertFalse(root.exists())

    def test_argument_validation(self):
        for action in [None, {}, {'tool': [], 'args': {}}, {'tool': 'push', 'args': {}},
                       {'tool': 'read_file', 'args': {'path': 42}},
                       {'tool': 'read_file', 'args': {'path': 'a', 'extra': 'b'}},
                       {'tool': 'bash', 'args': {'command': 'x' * 12001}}]:
            with self.subTest(action=action):
                self.assertEqual(self.runtime.execute(action)['status'], 'error')

    def test_paths(self):
        for path in ['../x', '/tmp/x', 'a/../../x', '.git/config', '', '.env',
                     'a/.git/config', 'a\\..\\x', 'C:foo', 'a\0b']:
            with self.subTest(path=path):
                self.assertEqual(self.call('write_file', path=path, content='x')['status'], 'denied')

    def test_symlinks(self):
        try:
            (self.runtime.root / 'link').symlink_to(self.source / 'untracked.txt')
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")
        self.assertEqual(self.call('read_file', path='link')['status'], 'denied')
        self.assertEqual(self.call('list_files')['output'], ['a.py'])

    def test_file_workflow(self):
        self.assertEqual(self.call('write_file', path='notes/a', content='hello')['status'], 'ok')
        self.assertEqual(self.call('search_files', query='hello')['output'][0]['path'], 'notes/a')
        self.assertEqual(self.call('edit_file', path='a.py', old='1', new='(')['status'], 'error')
        self.assertEqual(self.call('read_file', path='a.py')['output'], 'value = 1\n')
        self.assertEqual(self.call('delete_file', path='notes/a')['status'], 'ok')

    def test_bash_permissions(self):
        with mock.patch.object(self.runtime.sandbox, 'run', return_value={'exit_code': 3}) as run:
            self.assertEqual(self.call('bash', command='exit 3')['status'], 'denied')
            self.runtime.approve = mock.Mock(return_value='yes')
            self.assertEqual(self.call('bash', command='exit 3')['status'], 'denied')
            run.assert_not_called()
            self.runtime.approve.return_value = True
            self.assertEqual(self.call('bash', command='exit 3')['output']['exit_code'], 3)
            run.assert_called_once_with('exit 3', 20, 12000)
            self.runtime.tools = []
            self.runtime.approve.reset_mock()
            self.assertEqual(self.call('bash', command='exit 3')['status'], 'error')
            self.runtime.approve.assert_not_called()

    def test_failed_cleanup_blocks_all_actions(self):
        self.runtime.sandbox.broken = True
        try:
            for tool, args in [('read_file', {'path': 'a.py'}),
                               ('write_file', {'path': 'x', 'content': 'x'}),
                               ('bash', {'command': 'true'})]:
                self.assertEqual(self.call(tool, **args)['status'], 'denied')
        finally:
            self.runtime.sandbox.broken = False

    def test_url_allowlist(self):
        for url in ['http://docs.python.org/', 'https://example.com/',
                    'https://u:p@docs.python.org/', 'https://docs.python.org:8443/',
                    'https://docs.python.org/?q=x', 'https://[']:
            self.assertEqual(self.call('fetch_url', url=url)['status'], 'denied')


    def test_search_ignores_special_files(self):
        import os
        if not hasattr(os, 'mkfifo'):
            self.skipTest('named pipes are unavailable on this platform')
        os.mkfifo(self.runtime.root / 'pipe')
        self.assertEqual(self.call('search_files', query='value')['output'][0]['path'], 'a.py')

class SnapshotTest(unittest.TestCase):
    def test_excludes_credentials_and_links(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            dest = source / 'copy'
            dest.mkdir()
            names = ['ok.py', '.env', 'credentials.json', 'private.key', '.git/config']
            for name in names:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('example')
            with mock.patch('coding_harness.sandbox.subprocess.run', return_value=
                            subprocess.CompletedProcess([], 0, '\0'.join(names).encode())):
                snapshot_repository(source, dest)
            self.assertEqual([p.name for p in dest.iterdir()], ['ok.py'])
            try:
                (source / 'link').symlink_to(source / 'ok.py')
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            with mock.patch('coding_harness.sandbox.subprocess.run', return_value=
                            subprocess.CompletedProcess([], 0, b'link\0')):
                with self.assertRaises(SandboxError):
                    snapshot_repository(source, dest)


class DockerTest(unittest.TestCase):
    def setUp(self):
        self.sandbox = DockerSandbox(Path('/disposable/workspace'))
        patch = mock.patch.object(self.sandbox, '_control', return_value=
                                 subprocess.CompletedProcess([], 0, b'0\n'))
        self.control = patch.start()
        self.addCleanup(patch.stop)
        self.child = mock.Mock()
        self.child.poll.return_value = 0
        patch = mock.patch('coding_harness.sandbox.subprocess.Popen', return_value=self.child)
        self.popen = patch.start()
        self.addCleanup(patch.stop)

    def assert_removed(self):
        self.assertEqual(self.control.call_args.args[:2], ('rm', '--force'))

    def test_permissions_and_cleanup_on_success(self):
        self.sandbox.run('echo hi', 20, 12000)
        args = self.control.call_args_list[0].args
        for flag in ['--network=none', '--read-only', '--cap-drop=ALL', '--pull=never',
                     '--security-opt=no-new-privileges', '--user=65534:65534',
                     '--env=HOME=/tmp', '--entrypoint=/usr/bin/timeout']:
            self.assertIn(flag, args)
        self.assertEqual(args[args.index('--mount') + 1],
                         'type=bind,source=/disposable/workspace,target=/workspace')
        self.assertEqual(self.popen.call_args.args[0][:2], ['docker', 'start'])
        self.assert_removed()

    def test_deadline(self):
        self.child.poll.return_value = None
        with mock.patch('coding_harness.sandbox.time.monotonic', side_effect=[0, 21]):
            result = self.sandbox.run('sleep 30', 20, 12000)
        self.assertIn('timed out', result['stopped'])
        self.assert_removed()

    def test_output_limit(self):
        def launch(*args, **kwargs):
            kwargs['stdout'].write(b'x' * 12001)
            kwargs['stdout'].flush()
            return self.child
        self.popen.side_effect = launch
        result = self.sandbox.run('yes', 20, 12000)
        self.assertIn('exceeded', result['stopped'])
        self.assertEqual(len(result['output']), 12000)
        self.assert_removed()

    def test_interrupt_and_errors(self):
        for error in [KeyboardInterrupt(), OSError('attach failed')]:
            self.popen.side_effect = error
            with self.assertRaises(type(error)):
                self.sandbox.run('true', 20, 12000)
            self.assert_removed()

    def test_cleanup_failure(self):
        def control(*args):
            if args[0] == 'rm':
                raise subprocess.TimeoutExpired('docker', 15)
            return subprocess.CompletedProcess([], 0, b'0\n')
        self.control.side_effect = control
        with self.assertRaises(SandboxError):
            self.sandbox.run('true', 20, 12000)
        self.assertTrue(self.sandbox.broken)
        with self.assertRaises(SandboxError):
            self.sandbox.run('true', 20, 12000)


if __name__ == '__main__':
    unittest.main()
