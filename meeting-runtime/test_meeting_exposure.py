"""What a meeting container is given and who can reach it: the meeting view's token, the
container's environment, /health on this computer, and what .env may not choose."""
import asyncio
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from meeting_view import TOKEN, start_meeting_view, viewer_command, viewer_token, write_token_file

RUNTIME = Path(__file__).resolve().parent
CODE_ROOT = RUNTIME.parent


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class MeetingViewTests(unittest.IsolatedAsyncioTestCase):
    def test_the_launchers_token_is_used(self):
        token = 'A1_-' * 11
        self.assertEqual(viewer_token({'SMITLINE_VIEWER_TOKEN': f' {token}\n'}), token)

    def test_without_a_usable_token_the_view_gets_one_nobody_knows(self):
        for value in (None, '', 'short', 'x' * 129, 'with spaces ' * 4, 'colon:' * 8,
                      'a' * 40 + '\n127.0.0.1:22'):
            environ = {} if value is None else {'SMITLINE_VIEWER_TOKEN': value}
            first, second = viewer_token(environ), viewer_token(environ)
            self.assertTrue(TOKEN.fullmatch(first), value)
            self.assertNotEqual(first, second, value)
            self.assertNotIn(first, str(value))

    def test_the_token_file_is_private_and_names_only_the_display(self):
        with tempfile.TemporaryDirectory() as temp:
            path = write_token_file(temp, 'T' * 43)
            self.assertEqual(mode(path), 0o600)
            self.assertEqual(Path(path).read_text(), 'T' * 43 + ': 127.0.0.1:5900\n')

    def test_the_view_always_runs_behind_the_token_plugin(self):
        command = viewer_command('/tmp/view/view-token')
        self.assertEqual(command[:2], ['/usr/bin/websockify', '--web=/usr/share/novnc'])
        self.assertIn('--token-plugin=ReadOnlyTokenFile', command)
        self.assertIn('--token-source=/tmp/view/view-token', command)
        # No fixed target: a WebSocket without the token has nowhere to go.
        self.assertNotIn('127.0.0.1:5900', command)
        self.assertEqual(command[-1], '6080')

    async def test_start_and_stop_keep_the_token_out_of_the_environment_and_remove_it(self):
        calls = []

        class Process:
            returncode = None

            def terminate(self):
                self.returncode = -15

            async def wait(self):
                return self.returncode

        async def spawn(*command, **kwargs):
            calls.append((command, kwargs))
            return Process()
        token = 'k' * 43
        view = await start_meeting_view({'PATH': '/usr/bin'}, environ={'SMITLINE_VIEWER_TOKEN': token},
                                        spawn=spawn)
        [(command, kwargs)] = calls
        self.assertEqual(kwargs['env'], {'PATH': '/usr/bin'})
        self.assertNotIn(token, ' '.join(command))
        source = Path(next(arg for arg in command if arg.startswith('--token-source='))
                      [len('--token-source='):])
        self.assertEqual(source.read_text(), token + ': 127.0.0.1:5900\n')
        self.assertEqual(mode(source.parent), 0o700)
        await view.stop()
        self.assertEqual(view.process.returncode, -15)
        self.assertFalse(source.parent.exists())

    async def test_a_view_that_cannot_start_leaves_no_token_behind(self):
        sources = []

        async def spawn(*command, **kwargs):
            sources.append(Path(next(arg for arg in command if arg.startswith('--token-source='))
                                [len('--token-source='):]))
            raise FileNotFoundError('websockify')
        with self.assertRaises(FileNotFoundError):
            await start_meeting_view({}, environ={}, spawn=spawn)
        self.assertFalse(sources[0].parent.exists())

    def test_every_meeting_view_starts_through_meeting_view(self):
        for path in sorted(RUNTIME.glob('*.py')):
            if path.name.startswith('test_') or path.name == 'meeting_view.py':
                continue
            self.assertNotIn('websockify', path.read_text(encoding='utf-8'), path.name)
        for name in ('bridge.py', 'google_account.py', 'teams_account.py'):
            text = (RUNTIME / name).read_text(encoding='utf-8')
            self.assertIn('from meeting_view import start_meeting_view', text, name)
            self.assertIn('await viewer.stop()', text, name)

    def test_the_vnc_server_listens_inside_the_container_only(self):
        source = (CODE_ROOT / 'joinly' / 'joinly' / 'providers' / 'browser' / 'devices' /
                  'virtual_display.py')
        if not source.is_file():
            source = Path('/opt/joinly/joinly/providers/browser/devices/virtual_display.py')
        self.assertIn('"-localhost"', source.read_text(encoding='utf-8'))


class HealthHostTests(unittest.IsolatedAsyncioTestCase):
    def test_which_host_headers_name_the_machine_by_address(self):
        from meeting_control import plain_host
        for host in ('127.0.0.1:8094', '127.0.0.1', 'localhost:8094', 'LocalHost', '[::1]:8094',
                     '[::1]', '10.128.0.7:8094', '::1', 'localhost:'):
            self.assertTrue(plain_host(host), host)
        for host in (None, '', 'other.example:8094', 'other.example', '127.0.0.1.nip.io:8094',
                     'localhost.other.example', '127.0.0.1:x', '[::1]x', '[::1]:80x', 'local host'):
            self.assertFalse(plain_host(host), host)

    async def client(self, token_required):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from meeting_control import MeetingControl, health_route
        control = MeetingControl('meeting-token', {}, owner='Robin')
        app = web.Application()
        app.router.add_get('/health', health_route(
            control, lambda: {'captions': ['the quarterly numbers']}, token_required=token_required))
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_health_here_answers_only_requests_to_an_address(self):
        client = await self.client(token_required=False)
        for host in ('other.example:8094', '127.0.0.1.nip.io:8094'):
            response = await client.get('/health', headers={'Host': host})
            self.assertEqual(response.status, 403, host)
            self.assertNotIn('quarterly', await response.text())
        for host in (None, 'localhost:8094', '127.0.0.1:8094'):
            response = await client.get('/health', headers={'Host': host} if host else None)
            self.assertEqual(response.status, 200, host)
            self.assertIn('quarterly', await response.text())

    async def test_without_a_meeting_health_answers_only_this_computer_too(self):
        # As the Google and Teams sign-in browsers serve it: no meeting control, no token.
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from meeting_control import health_route
        app = web.Application()
        app.router.add_get('/health', health_route(None, lambda: {'stage': 'connecting_account'}))
        client = TestClient(TestServer(app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        self.assertEqual((await client.get('/health', headers={'Host': 'other.example:8094'})).status, 403)
        response = await client.get('/health')
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {'stage': 'connecting_account'})

    def test_the_sign_in_browsers_serve_health_through_the_same_route(self):
        for name in ('google_account.py', 'teams_account.py'):
            text = (RUNTIME / name).read_text(encoding='utf-8')
            self.assertIn("app.router.add_get('/health', health_route(None, lambda: state))", text, name)
            self.assertNotIn('async def health(', text, name)

    async def test_health_on_a_network_goes_by_the_token_whatever_the_machine_is_called(self):
        client = await self.client(token_required=True)
        self.assertEqual((await client.get('/health')).status, 403)
        response = await client.get('/health', headers={'Host': 'meeting-vm.internal:8094',
                                                        'Authorization': 'Bearer meeting-token'})
        self.assertEqual(response.status, 200)


class ViewTokenFileTests(unittest.IsolatedAsyncioTestCase):
    """Whoever starts a meeting container makes its view token and keeps it in a private file;
    it is gone once the meeting is."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'meeting-runtime'
        self.runtime.mkdir()

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def supervisor(self, launcher, claim=None):
        from meeting_supervisor import ProductionMeetingSupervisor
        from test_meeting_supervisor import FakeDaemon, FakeHealth
        supervisor = ProductionMeetingSupervisor(
            self.root, runtime_root=self.runtime, launcher=launcher, health=FakeHealth(),
            poll_interval=0.02, start_timeout=0.4, stop_timeout=0.4, claim=claim,
            owner_environment=lambda owner: {'OPENAI_API_KEY': 'sk-meeting'}).bind_daemon(FakeDaemon())
        self.addAsyncCleanup(supervisor.shutdown)
        return supervisor

    async def test_the_one_container_gets_a_new_token_for_each_start(self):
        from runtime_state import viewer_token_path
        from test_meeting_supervisor import FakeLauncher, session
        launcher = FakeLauncher()
        supervisor = self.supervisor(launcher)
        path = viewer_token_path(self.root)
        tokens = []
        for meeting_id in ('mtg-first00000001', 'mtg-second0000001'):
            await supervisor.start(session(id=meeting_id))
            token = launcher.ups[-1]['SMITLINE_VIEWER_TOKEN']
            self.assertTrue(TOKEN.fullmatch(token))
            self.assertEqual(path.read_text(), token + '\n')
            self.assertEqual(mode(path), 0o600)
            await supervisor.cancel(meeting_id)
            self.assertFalse(path.exists())
            tokens.append(token)
        self.assertNotEqual(tokens[0], tokens[1])

    async def test_a_failed_start_leaves_no_view_token(self):
        from runtime_state import viewer_token_path
        from test_meeting_supervisor import FakeLauncher, session
        launcher = FakeLauncher()
        launcher.fail_up = True
        supervisor = self.supervisor(launcher)
        with self.assertRaises(Exception):
            await supervisor.start(session())
        self.assertFalse(viewer_token_path(self.root).exists())

    async def test_a_container_started_without_an_environment_gets_no_key(self):
        # No owner_environment: no env file, so compose reads /dev/null and nothing of .env.
        from meeting_supervisor import ProductionMeetingSupervisor
        from test_meeting_supervisor import FakeDaemon, FakeHealth, FakeLauncher, session
        launcher = FakeLauncher()
        supervisor = ProductionMeetingSupervisor(
            self.root, runtime_root=self.runtime, launcher=launcher, health=FakeHealth(),
            poll_interval=0.02, start_timeout=0.4, stop_timeout=0.4).bind_daemon(FakeDaemon())
        self.addAsyncCleanup(supervisor.shutdown)
        await supervisor.start(session())
        self.assertNotIn('SMITLINE_MEETING_ENV_FILE', launcher.ups[0])
        self.assertNotIn('OPENAI_API_KEY', launcher.ups[0])
        await supervisor.cancel(session().id)

    async def test_a_pool_meeting_keeps_its_token_beside_its_claim(self):
        from runtime_state import pool_claim_path, viewer_token_path
        from test_meeting_supervisor import FakeLauncher, session
        launcher = FakeLauncher()
        meeting = session(id='mtg-pool000000001')
        claim = pool_claim_path(self.root, meeting.id)
        supervisor = self.supervisor(launcher, claim={'path': claim, 'record': {'owner': 'local'}})
        await supervisor.start(meeting)
        view = claim.with_suffix('.view')
        self.assertEqual(view.read_text(), launcher.ups[0]['SMITLINE_VIEWER_TOKEN'] + '\n')
        self.assertEqual(mode(view), 0o600)
        # The console's Open meeting view shows only the one container.
        self.assertFalse(viewer_token_path(self.root).exists())
        await supervisor.cancel(meeting.id)
        self.assertFalse(view.exists())
        self.assertFalse(claim.exists())


class MeetingEnvironmentTests(unittest.TestCase):
    """One meeting container at a time gets what a pool meeting gets, plus the passcode the
    console saves; never the rest of .env or .env.meeting."""

    def setUp(self):
        from test_meeting_pool import INSTALLATION_SECRETS
        self.secrets = INSTALLATION_SECRETS
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        dotenv = {**INSTALLATION_SECRETS, 'OPENAI_API_KEY': 'sk-proj-installation',
                  'SMITLINE_VOICE': 'cinder', 'SMITLINE_OWNER_NAME': 'Robin'}
        (self.root / '.env').write_text(''.join(f'{key}={value}\n' for key, value in dotenv.items()))
        (self.root / '.env.meeting').write_text(
            'MEETING_URL=https://zoom.us/j/111111111\nMEETING_PASSCODE=pass$word\n'
            'SMITLINE_MEETING_INSTRUCTIONS=Take notes only.\nSMITLINE_AUTH_MODE=google\n'
            'SIGNALWIRE_API_TOKEN=PTsw-in-env-meeting-secret\n')

    def tearDown(self):
        self.temporary.cleanup()

    def assert_no_secret(self, text):
        for name, value in self.secrets.items():
            self.assertNotIn(value, text, name)
        self.assertNotIn('PTsw-in-env-meeting-secret', text)
        self.assertNotIn('SMITLINE_AUTH_MODE', text)

    def test_the_daemons_meeting_gets_the_passcode_but_takes_its_address_from_the_state(self):
        from call_hooks import DefaultCallHooks
        from meeting_line import single_meeting_environment
        hooks = DefaultCallHooks(environ={}, env_file=self.root / '.env')
        values = single_meeting_environment(hooks, 'local', self.root / '.env.meeting')
        self.assertEqual(values, {
            'OPENAI_API_KEY': 'sk-proj-installation', 'SMITLINE_VOICE': 'cinder',
            'SMITLINE_OWNER_NAME': 'Robin', 'SMITLINE_MEETING_INSTRUCTIONS': 'Take notes only.',
            'MEETING_PASSCODE': 'pass$word'})
        by_hand = single_meeting_environment(hooks, 'local', self.root / '.env.meeting', by_hand=True)
        self.assertEqual(by_hand, {**values, 'MEETING_URL': 'https://zoom.us/j/111111111'})
        self.assert_no_secret(repr(by_hand))

    def test_without_env_meeting_only_the_installations_own_settings(self):
        from call_hooks import DefaultCallHooks
        from meeting_line import single_meeting_environment
        (self.root / '.env.meeting').unlink()
        hooks = DefaultCallHooks(environ={}, env_file=self.root / '.env')
        values = single_meeting_environment(hooks, 'local', self.root / '.env.meeting', by_hand=True)
        self.assertEqual(values, {'OPENAI_API_KEY': 'sk-proj-installation', 'SMITLINE_VOICE': 'cinder',
                                  'SMITLINE_OWNER_NAME': 'Robin'})

    def test_the_daemon_gives_the_one_container_only_what_it_reads(self):
        from daemon_main import build_app, build_parser
        # Every secret but a hooks plugin, which the daemon would load.
        dotenv = {key: value for key, value in self.secrets.items() if key != 'SMITLINE_CALL_HOOKS'}
        dotenv.update(OPENAI_API_KEY='sk-proj-installation', SMITLINE_VOICE='cinder')
        (self.root / '.env').write_text(''.join(f'{key}={value}\n' for key, value in dotenv.items()))
        args = build_parser().parse_args(['--root', str(self.root),
                                          '--runtime-root', str(self.root / 'meeting-runtime')])
        with mock.patch.dict(os.environ):
            for name in ('OPENAI_API_KEY', 'SMITLINE_MAX_MEETINGS', 'SMITLINE_REQUIRE_OWNER',
                         'SMITLINE_MEETING_LAUNCHER'):
                os.environ.pop(name, None)
            app, _host, _port, _token = build_app(args)
        try:
            self.assertTrue(app.meeting_supervisor.local_only)
            values = app.meeting_supervisor.owner_environment('local')
        finally:
            app.runtime_daemon.close()
        self.assertEqual(values['OPENAI_API_KEY'], 'sk-proj-installation')
        self.assertEqual(values['MEETING_PASSCODE'], 'pass$word')
        self.assertNotIn('MEETING_URL', values)
        self.assert_no_secret(repr(values))

    def test_a_meeting_started_by_hand_gets_a_private_file_and_nothing_printed(self):
        import contextlib
        import io
        from meeting_env import main
        from test_owner_isolation import parse_env_file
        target = self.root / 'private' / 'meeting.env'
        target.parent.mkdir(mode=0o700)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': ''}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main([str(self.root), str(target)]), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))
        self.assertEqual(mode(target), 0o600)
        body = target.read_text()
        self.assert_no_secret(body)
        values = parse_env_file(body)
        self.assertEqual(values['MEETING_URL'], 'https://zoom.us/j/111111111')
        self.assertEqual(values['MEETING_PASSCODE'], 'pass$word')
        self.assertEqual(values['OPENAI_API_KEY'], 'sk-proj-installation')
        # It never writes over a file that is already there.
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(FileExistsError):
            main([str(self.root), str(target)])

    def test_without_an_openai_key_nothing_is_written(self):
        import contextlib
        import io
        from meeting_env import main
        (self.root / '.env').write_text('TWILIO_AUTH_TOKEN=tw-auth-secret\n')
        target = self.root / 'meeting.env'
        err = io.StringIO()
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': ''}), contextlib.redirect_stderr(err):
            self.assertEqual(main([str(self.root), str(target)]), 1)
        self.assertIn('OPENAI_API_KEY', err.getvalue())
        self.assertFalse(target.exists())


class TunnelProgramTests(unittest.TestCase):
    """.env never chooses a program the daemon runs."""

    def tunnel(self, settings, own, which):
        from tunnel import PublicUrl
        logs = []
        url = PublicUrl(lambda: settings, 8766, which=which, own_environ=own,
                        log=lambda line, **_: logs.append(line))
        return url, logs

    def test_what_the_daemon_runs_is_never_chosen_by_the_env_file(self):
        settings = {'SMITLINE_CLOUDFLARED': '/data/meetings/recordings/other',
                    'SMITLINE_CLOUDFLARED_IMAGE': 'other/image:latest'}
        docker_only = lambda name: '/usr/bin/docker' if name == 'docker' else None
        url, logs = self.tunnel(settings, {}, docker_only)
        command = url.command()
        self.assertEqual(command[0], 'docker')
        self.assertNotIn('other/image:latest', command)
        self.assertNotIn('/data/meetings/recordings/other', command)
        url.command()
        self.assertEqual(len(logs), 2)  # once for each setting
        self.assertTrue(all('in .env is ignored' in line for line in logs))

    def test_the_daemons_own_environment_still_chooses(self):
        own = {'SMITLINE_CLOUDFLARED': '/opt/cloudflared/bin/cloudflared'}
        url, logs = self.tunnel({}, own, lambda name: None)
        self.assertEqual(url.command()[0], '/opt/cloudflared/bin/cloudflared')
        url, _ = self.tunnel({}, {'SMITLINE_CLOUDFLARED_IMAGE': 'mirror.example/cloudflared:2026.10.3'},
                             lambda name: '/usr/bin/docker' if name == 'docker' else None)
        self.assertIn('mirror.example/cloudflared:2026.10.3', url.command())
        self.assertEqual(logs, [])


if __name__ == '__main__':
    unittest.main()
