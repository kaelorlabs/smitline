import unittest
from datetime import datetime, timedelta, timezone

from agent_sessions import MeetingPermissions
from hosted_runtime import (
    FORBIDDEN_REMOTE_OPS, PROTOCOL_VERSION, ControlPlane, FakeTlsSession, HostedRuntime,
    HostedRuntimeError, InMemoryRemoteControlPlane, LocalRunner, RunnerTransport,
    filter_remote_event, narrow_permissions, workspace_identity_from_path,
)


def perms(**overrides):
    payload = {
        'workspace': 'read-only',
        'commands': 'approval-required',
        'edits': 'disabled',
        'network': 'approval-required',
        'commits': 'disabled',
        'pushes': 'disabled',
    }
    payload.update(overrides)
    return MeetingPermissions.from_dict(payload)


def job(device_id, **overrides):
    payload = {
        'assignmentId': 'asg-one',
        'tenantId': 'ten-a',
        'userId': 'usr-a',
        'deviceId': device_id,
        'provider': 'codex',
        'sessionId': 'thread-origin-1',
        'workspaceIdentity': 'ws-abc123',
        'permissions': perms().to_dict(),
        'meetingId': 'mtg-hosted00000001',
        'op': 'assign_meeting',
    }
    payload.update(overrides)
    return payload


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


class HostedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.plane = InMemoryRemoteControlPlane(clock=self.clock, pairing_ttl=30, heartbeat_timeout=10)

    def pair(self, tenant='ten-a', user='usr-a'):
        started = self.plane.start_pairing(tenant_id=tenant, user_id=user)
        return self.plane.complete_pairing(started['pairingId'], started['pairingCode'])

    def test_pairing_expiry_replay_and_revocation(self):
        started = self.plane.start_pairing(tenant_id='ten-a', user_id='usr-a')
        self.assertIn('pairingCode', started)
        self.clock.advance(31)
        with self.assertRaises(HostedRuntimeError) as expired:
            self.plane.complete_pairing(started['pairingId'], started['pairingCode'])
        self.assertEqual(expired.exception.code, 'pairing_expired')
        fresh = self.plane.start_pairing(tenant_id='ten-a', user_id='usr-a')
        first = self.plane.complete_pairing(fresh['pairingId'], fresh['pairingCode'])
        with self.assertRaises(HostedRuntimeError) as replay:
            self.plane.complete_pairing(fresh['pairingId'], fresh['pairingCode'])
        self.assertEqual(replay.exception.code, 'pairing_replay')
        self.plane.revoke_device(first['deviceId'])
        with self.assertRaises(HostedRuntimeError) as revoked:
            self.plane.authenticate_device(first['deviceId'], first['deviceEnrollment'])
        self.assertEqual(revoked.exception.code, 'device_revoked')
        dumped = str(self.plane.audit) + str(self.plane.devices[first['deviceId']])
        self.assertNotIn(fresh['pairingCode'], dumped)
        self.assertNotIn(first['deviceEnrollment'], dumped)

    def test_tenant_separation(self):
        a = self.pair('ten-a', 'usr-a')
        b = self.pair('ten-b', 'usr-b')
        assigned = self.plane.assign_job(job(a['deviceId'], tenantId='ten-a'))
        with self.assertRaises(HostedRuntimeError) as isolated:
            self.plane.assign_job(job(a['deviceId'], tenantId='ten-b', assignmentId='asg-cross'))
        self.assertEqual(isolated.exception.code, 'tenant_isolation')
        self.plane.upload_events(a['deviceId'], [{
            'id': 'evt-a', 'type': 'meeting.live', 'meetingId': assigned['meetingId'],
        }])
        replay_b = self.plane.reconnect(b['deviceId'])
        self.assertEqual(replay_b['replay'], [])

    def test_heartbeat_loss_and_reconnect_cursors(self):
        paired = self.pair()
        self.plane.register_runner(paired['deviceId'])
        self.plane.heartbeat(paired['deviceId'])
        self.plane.upload_events(paired['deviceId'], [
            {'id': 'evt-1', 'type': 'meeting.joining', 'meetingId': 'mtg-1'},
            {'id': 'evt-2', 'type': 'meeting.live', 'meetingId': 'mtg-1'},
        ])
        self.clock.advance(11)
        status = self.plane.runner_status(paired['deviceId'])
        self.assertFalse(status['runnerOnline'])
        replay = self.plane.reconnect(paired['deviceId'], 'evt-1')
        self.assertEqual([item['id'] for item in replay['replay']], ['evt-2'])
        self.assertTrue(replay['online'])

    def test_duplicate_assignment_cancellation_and_handoff_once(self):
        paired = self.pair()
        first = self.plane.assign_job(job(paired['deviceId']))
        second = self.plane.assign_job(job(paired['deviceId']))
        self.assertEqual(first['assignmentId'], second['assignmentId'])
        self.assertEqual(list(self.plane.jobs), ['asg-one'])
        cancelled = self.plane.cancel_job('asg-one')
        self.assertEqual(cancelled['status'], 'cancelled')
        again = self.plane.cancel_job('asg-one')
        self.assertEqual(again['status'], 'cancelled')
        with self.assertRaises(HostedRuntimeError) as blocked:
            self.plane.complete_handoff('asg-one', 'hnd-1')
        self.assertEqual(blocked.exception.code, 'assignment_cancelled')
        self.assertEqual(self.plane.jobs['asg-one']['status'], 'cancelled')

    def test_one_active_assignment_per_meeting_session(self):
        paired = self.pair()
        first = self.plane.assign_job(job(paired['deviceId'], assignmentId='asg-1'))
        second = self.plane.assign_job(job(paired['deviceId'], assignmentId='asg-2'))
        self.assertEqual(first['assignmentId'], second['assignmentId'])
        self.assertEqual(list(self.plane.jobs), ['asg-1'])

    def test_handoff_appends_once_for_a_live_assignment(self):
        paired = self.pair()
        self.plane.assign_job(job(
            paired['deviceId'], assignmentId='asg-live',
            meetingId='mtg-hosted00000008', sessionId='thread-live',
        ))
        one = self.plane.complete_handoff('asg-live', 'hnd-1')
        two = self.plane.complete_handoff('asg-live', 'hnd-other')
        self.assertEqual(one, two)
        self.assertEqual(two['handoffId'], 'hnd-1')

    def test_pairing_survives_process_restart(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            first = HostedRuntime(Path(directory) / 'hosted', clock=self.clock)
            started = first.start_pair(tenant_id='ten-a', user_id='usr-a')
            completed = first.complete_pair(started['pairingId'], started['pairingCode'])
            self.assertTrue(first.status()['paired'])
            second = HostedRuntime(Path(directory) / 'hosted', clock=self.clock)
            status = second.status()
            self.assertTrue(status['paired'])
            self.assertEqual(status['deviceId'], completed['deviceId'])
            self.assertNotEqual(status.get('error'), 're_pair_required')

    def test_events_and_jobs_are_bound_to_the_authenticating_device(self):
        a = self.pair('ten-a', 'usr-a')
        started_b = self.plane.start_pairing(tenant_id='ten-a', user_id='usr-b')
        b = self.plane.complete_pairing(started_b['pairingId'], started_b['pairingCode'])
        self.plane.register_runner(a['deviceId'])
        self.plane.register_runner(b['deviceId'])
        self.plane.upload_events(a['deviceId'], [
            {'id': 'evt-a', 'type': 'meeting.lifecycle', 'state': 'live'},
        ])
        replay = self.plane.reconnect(b['deviceId'], cursor='')
        self.assertNotIn('evt-a', [item.get('id') for item in replay.get('replay') or []])
        runner = LocalRunner(self.plane, local_permissions=perms(), device_id=a['deviceId'])
        with self.assertRaises(HostedRuntimeError) as isolated:
            runner.accept_job(job(b['deviceId'], assignmentId='asg-foreign', userId='usr-b',
                                  sessionId='thread-origin-9', meetingId='mtg-hosted00000003'))
        self.assertEqual(isolated.exception.code, 'device_isolation')

    def test_permission_narrowing_and_local_enforcement(self):
        local = perms(workspace='read-only', commands='approval-required')
        narrower = perms(workspace='none', commands='disabled', network='disabled')
        self.assertEqual(narrow_permissions(local, narrower).workspace, 'none')
        with self.assertRaises(HostedRuntimeError) as escalated:
            narrow_permissions(local, perms(workspace='workspace-write'))
        self.assertEqual(escalated.exception.code, 'permission_escalation')
        runner = LocalRunner(self.plane, local_permissions=local)
        accepted = runner.accept_job(job('dev-x', permissions=narrower.to_dict()))
        self.assertEqual(accepted['permissions']['workspace'], 'none')
        with self.assertRaises(HostedRuntimeError):
            runner.accept_job(job('dev-x', assignmentId='asg-esc', permissions=perms(commands='allowed').to_dict()))
        with self.assertRaises(HostedRuntimeError) as incomplete:
            runner.accept_job({'op': 'assign_meeting', 'meetingId': 'mtg-1'})
        self.assertEqual(incomplete.exception.code, 'job_binding_incomplete')
        for op in FORBIDDEN_REMOTE_OPS:
            with self.subTest(op=op), self.assertRaises(HostedRuntimeError) as forbidden:
                runner.accept_job(job('dev-x', assignmentId='asg-' + op, op=op))
            self.assertEqual(forbidden.exception.code, 'forbidden_remote_op')
        with self.assertRaises(HostedRuntimeError) as path:
            self.plane.assign_job(job('dev-x', workspaceIdentity='/tmp/secret-repo'))
        self.assertEqual(path.exception.code, 'forbidden_remote_op')

    def test_event_filter_redacts_transcripts_bytes_and_paths(self):
        allowed = filter_remote_event({
            'id': 'evt-live',
            'type': 'meeting.live',
            'meetingId': 'mtg-1',
            'entry': 'never upload speech',
        })
        self.assertEqual(allowed['type'], 'meeting.live')
        self.assertNotIn('entry', allowed)
        self.assertIsNone(filter_remote_event({'id': 'evt-t', 'type': 'transcript.delta', 'entry': 'secret talk'}))
        self.assertIsNone(filter_remote_event({
            'id': 'evt-obs', 'type': 'screen_share.observation', 'observation': 'whiteboard',
        }))
        artifact = filter_remote_event({
            'id': 'evt-art',
            'type': 'artifact.created',
            'artifact': {
                'id': 'art-1', 'type': 'diff', 'size': 12, 'createdAt': '2026-09-17T12:00:00Z',
                'path': '/tmp/work/diff.patch', 'bytes': 'AAAA',
            },
        })
        self.assertEqual(artifact['artifact']['id'], 'art-1')
        self.assertNotIn('path', artifact['artifact'])
        self.assertNotIn('bytes', artifact['artifact'])
        handoff = filter_remote_event({
            'id': 'evt-h',
            'type': 'handoff.ready',
            'handoffId': 'hnd-1',
            'summary': 'discussed the customer token',
            'transcriptPath': '/tmp/transcript.jsonl',
        })
        self.assertEqual(handoff['handoffId'], 'hnd-1')
        self.assertNotIn('summary', handoff)
        self.assertNotIn('transcriptPath', handoff)

    def test_fake_tls_nonce_replay_and_key_rotation(self):
        key = b'test-transport-key-material'
        sender = FakeTlsSession(key, 'k1')
        receiver = FakeTlsSession(key, 'k1')
        envelope = sender.seal({'protocolVersion': PROTOCOL_VERSION, 'kind': 'runner.heartbeat'})
        opened = receiver.open(envelope)
        self.assertEqual(opened['kind'], 'runner.heartbeat')
        with self.assertRaises(HostedRuntimeError) as replay:
            receiver.open(envelope)
        self.assertEqual(replay.exception.code, 'replayed_nonce')
        new_id = sender.rotate(b'rotated-key-material', 'k2')
        self.assertEqual(new_id, 'k2')
        receiver.rotate(b'rotated-key-material', 'k2')
        rotated = sender.seal({'protocolVersion': PROTOCOL_VERSION, 'kind': 'runner.register'})
        self.assertEqual(receiver.open(rotated)['kind'], 'runner.register')

    def test_hosted_facade_persists_hashes_not_codes(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            runtime = HostedRuntime(Path(directory) / 'hosted', clock=self.clock)
            started = runtime.start_pair(tenant_id='ten-local', user_id='usr-local')
            completed = runtime.complete_pair(started['pairingId'], started['pairingCode'])
            status = runtime.status()
            self.assertTrue(status['paired'])
            self.assertEqual(status['controlPlane'], 'mock-remote')
            self.assertNotIn('pairingCode', status)
            self.assertNotIn('deviceEnrollment', status)
            stored = (Path(directory) / 'hosted' / 'runner-state.json').read_text()
            self.assertNotIn(started['pairingCode'], stored)
            self.assertNotIn(completed['deviceEnrollment'], stored)
            runtime.unpair()
            self.assertFalse(runtime.status()['paired'])

    def test_workspace_identity_is_not_a_path(self):
        identity = workspace_identity_from_path('/tmp/colleague-workspace')
        self.assertTrue(identity.startswith('ws-'))
        self.assertNotIn('/', identity)

    def test_loopback_is_default_control_plane_and_transport(self):
        self.assertEqual(ControlPlane.protocol_version, PROTOCOL_VERSION)
        self.assertEqual(RunnerTransport.protocol_version, PROTOCOL_VERSION)
        self.assertIsInstance(self.plane, ControlPlane)
        self.assertTrue(issubclass(FakeTlsSession, RunnerTransport))
        runner = LocalRunner(self.plane, local_permissions=perms())
        self.assertIsInstance(runner.transport, RunnerTransport)
        sealed = runner.send({'kind': 'runner.heartbeat'})
        self.assertEqual(sealed['protocolVersion'], PROTOCOL_VERSION)
        self.assertEqual(runner.receive(sealed)['kind'], 'runner.heartbeat')

    def test_rate_limit_rejects_additional_assignments(self):
        plane = InMemoryRemoteControlPlane(
            clock=self.clock, pairing_ttl=30, heartbeat_timeout=10, rate_limit=1)
        started = plane.start_pairing(tenant_id='ten-a', user_id='usr-a')
        paired = plane.complete_pairing(started['pairingId'], started['pairingCode'])
        first = plane.assign_job(job(paired['deviceId']))
        again = plane.assign_job(job(paired['deviceId']))
        self.assertEqual(first['assignmentId'], again['assignmentId'])
        with self.assertRaises(HostedRuntimeError) as limited:
            plane.assign_job(job(
                paired['deviceId'], assignmentId='asg-two',
                meetingId='mtg-hosted00000099', sessionId='thread-origin-2',
            ))
        self.assertEqual(limited.exception.code, 'rate_limited')


if __name__ == '__main__':
    unittest.main()
