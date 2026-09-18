import socket
import tempfile
import threading
import unittest
from pathlib import Path

from command_runner import run_command, validate_command
from workspace_isolation import IsolationError


class CommandRunnerIsolationTests(unittest.TestCase):
    def test_python_argv_cannot_write_outside_the_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'ws'
            workspace.mkdir()
            victim = root / 'outside.txt'
            with self.assertRaises(IsolationError):
                run_command(
                    ['python3', '-c', f'open({str(victim)!r},"w").write("escaped")'],
                    cwd=None, workspace=workspace, network_allowed=True,
                )
            self.assertFalse(victim.exists())

    def test_python_is_refused_when_network_is_disabled(self):
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        port = server.getsockname()[1]
        hit = {'ok': False}

        def accept():
            try:
                conn, _addr = server.accept()
                hit['ok'] = True
                conn.close()
            except Exception:
                pass

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'ws'
            workspace.mkdir()
            with self.assertRaises(IsolationError) as raised:
                run_command(
                    ['python3', '-c', (
                        'import socket\n'
                        f's=socket.create_connection(("127.0.0.1",{port}),2)\n'
                        's.sendall(b"x"); s.close(); print("NET_OK")\n'
                    )],
                    cwd=None, workspace=workspace, network_allowed=False, timeout=5,
                )
        server.close()
        thread.join(timeout=1)
        self.assertIn('network isolation', str(raised.exception))
        self.assertFalse(hit['ok'])

    def test_git_remote_set_url_is_rejected(self):
        with self.assertRaises(IsolationError):
            validate_command(['git', 'remote', 'set-url', 'origin', '/tmp/evil.git'])
        validate_command(['git', 'remote', '-v'])
        validate_command(['git', 'status'])
