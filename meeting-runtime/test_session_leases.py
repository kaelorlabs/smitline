import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_sessions import AgentSessionRef
from session_leases import (
    LeaseConflictError, LeaseOwnershipError, LeaseStateError, SessionLease,
    SessionLeaseStore, lease_digest,
)


TIMESTAMP = datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, moment=None):
        self.now = moment or TIMESTAMP

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


def session_ref(session_id='thread-origin-1', provider='codex'):
    return AgentSessionRef.from_dict({
        'provider': provider,
        'sessionId': session_id,
        'workspace': '/Users/Taylor/project',
        'model': 'gpt-5.6-terra',
    })


def _acquire_once(root, session_id, meeting_id, owner):
    store = SessionLeaseStore(root, owner_factory=lambda: owner)
    try:
        return store.acquire(session_ref(session_id), meeting_id).token
    except LeaseConflictError:
        return None


def _acquire_to_file(root, path, owner):
    token = _acquire_once(root, 'thread-b', 'mtg-b', owner)
    Path(path).write_text('' if token is None else token, encoding='utf-8')


class SessionLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.store = SessionLeaseStore(
            self.temporary.name,
            clock=self.clock,
            owner_factory=lambda: {'identity': 'test-owner', 'pid': 1, 'hostname': 'test'},
            heartbeat_timeout=30,
        )

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def acquire(self, session_id='thread-origin-1', meeting_id='mtg-a', provider='codex'):
        return self.store.acquire(session_ref(session_id, provider), meeting_id)

    def test_schema_round_trip_and_secret_absence(self):
        lease = self.acquire()
        parsed = SessionLease.from_dict(lease.to_dict())
        self.assertEqual(parsed.to_dict(), lease.to_dict())
        self.assertEqual(parsed.state, 'acquiring')
        self.assertEqual(parsed.provider, 'codex')
        path = self.store.root / (lease_digest('codex', 'thread-origin-1') + '.json')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)
        text = path.read_text(encoding='utf-8')
        payload = json.loads(text)
        self.assertNotIn('workspace', payload)
        self.assertNotIn('apiKey', text)
        self.assertNotIn('api_key', text)
        self.assertNotIn('/Users/Taylor/project', text)
        self.assertIn('leaseId', payload)
        self.assertNotIn('token', payload)
        self.assertNotIn('leaseToken', payload)

    def test_valid_and_invalid_transitions(self):
        lease = self.acquire()
        token = lease.token
        with self.assertRaises(LeaseStateError):
            self.store.release('codex', 'thread-origin-1', token)
        with self.assertRaises(LeaseStateError):
            self.store.start_turn('codex', 'thread-origin-1', token, 'turn-1')
        live = self.store.enter_meeting('codex', 'thread-origin-1', token)
        self.assertEqual(live.state, 'in_meeting')
        with self.assertRaises(LeaseStateError):
            self.store.enter_meeting('codex', 'thread-origin-1', token)
        with self.assertRaises(LeaseStateError):
            self.store.release('codex', 'thread-origin-1', token)
        with self.assertRaises(LeaseStateError):
            self.store.complete_finalization('codex', 'thread-origin-1', token)
        finishing = self.store.begin_finalization('codex', 'thread-origin-1', token)
        self.assertEqual(finishing.state, 'finalizing')
        self.assertEqual(finishing.finalization.status, 'in_progress')
        with self.assertRaises(LeaseStateError):
            self.store.release('codex', 'thread-origin-1', token)
        done = self.store.complete_finalization('codex', 'thread-origin-1', token)
        self.assertEqual(done.finalization.status, 'completed')
        self.store.release('codex', 'thread-origin-1', token)
        self.assertIsNone(self.store.get('codex', 'thread-origin-1'))

    def test_wrong_token_rejected(self):
        lease = self.acquire()
        with self.assertRaises(LeaseOwnershipError):
            self.store.enter_meeting('codex', 'thread-origin-1', 'not-the-token')
        self.assertEqual(self.store.get('codex', 'thread-origin-1').state, 'acquiring')
        self.store.fail('codex', 'thread-origin-1', lease.token)
        with self.assertRaises(LeaseOwnershipError):
            self.store.release('codex', 'thread-origin-1', 'not-the-token')

    def test_thread_and_process_acquisition_has_one_winner(self):
        barrier = threading.Barrier(8)
        results = []

        def run(_index):
            barrier.wait()
            store = SessionLeaseStore(
                self.store.root,
                owner_factory=lambda: {'identity': f'thread-{_index}'},
            )
            try:
                results.append(store.acquire(session_ref(), 'mtg-a').token)
            except LeaseConflictError:
                results.append(None)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(run, range(8)))
        self.assertEqual(sum(1 for token in results if token), 1)

        winners_dir = Path(self.temporary.name) / 'winners'
        winners_dir.mkdir()
        context = multiprocessing.get_context('spawn')
        processes = []
        for index in range(4):
            process = context.Process(
                target=_acquire_to_file,
                args=(str(self.store.root), str(winners_dir / f'{index}.txt'),
                      {'identity': f'p-{index}'}),
            )
            process.start()
            processes.append(process)
        for process in processes:
            process.join(timeout=15)
            self.assertEqual(process.exitcode, 0)
        tokens = [path.read_text(encoding='utf-8') for path in winners_dir.iterdir()]
        self.assertEqual(sum(1 for token in tokens if token), 1)

    def test_independent_sessions_and_restart_persistence(self):
        first = self.acquire('thread-a', 'mtg-a')
        second = self.acquire('thread-b', 'mtg-b')
        self.assertNotEqual(first.token, second.token)
        restarted = SessionLeaseStore(self.store.root, clock=self.clock)
        self.assertEqual(restarted.get('codex', 'thread-a').token, first.token)
        self.assertEqual(restarted.get('codex', 'thread-b').meeting_id, 'mtg-b')

    def test_delegated_turn_serialization(self):
        lease = self.acquire()
        self.store.enter_meeting('codex', 'thread-origin-1', lease.token)
        active = self.store.start_turn('codex', 'thread-origin-1', lease.token, 'turn-1')
        self.assertEqual(active.active_delegated_turn, 'turn-1')
        with self.assertRaises(LeaseConflictError):
            self.store.start_turn('codex', 'thread-origin-1', lease.token, 'turn-2')
        with self.assertRaises(LeaseConflictError):
            self.store.begin_finalization('codex', 'thread-origin-1', lease.token)
        self.store.finish_turn('codex', 'thread-origin-1', lease.token, 'turn-1')
        self.assertIsNone(self.store.get('codex', 'thread-origin-1').active_delegated_turn)
        self.store.start_turn('codex', 'thread-origin-1', lease.token, 'turn-2')

    def test_heartbeat_and_failed_release_paths(self):
        lease = self.acquire()
        self.clock.advance(10)
        beat = self.store.heartbeat('codex', 'thread-origin-1', lease.token)
        self.assertGreater(beat.last_heartbeat, lease.last_heartbeat)
        failed = self.store.fail('codex', 'thread-origin-1', lease.token, 'cancelled')
        self.assertEqual(failed.state, 'failed')
        self.store.release('codex', 'thread-origin-1', lease.token)
        self.assertIsNone(self.store.get('codex', 'thread-origin-1'))
        again = self.acquire()
        self.store.enter_meeting('codex', 'thread-origin-1', again.token)
        self.store.fail('codex', 'thread-origin-1', again.token, 'error')
        self.store.release('codex', 'thread-origin-1', again.token)

    def test_no_premature_release(self):
        lease = self.acquire()
        self.store.enter_meeting('codex', 'thread-origin-1', lease.token)
        with self.assertRaises(LeaseStateError):
            self.store.release('codex', 'thread-origin-1', lease.token)
        self.store.begin_finalization('codex', 'thread-origin-1', lease.token)
        with self.assertRaises(LeaseStateError):
            self.store.release('codex', 'thread-origin-1', lease.token)

    def test_stale_owner_alive_rejected_and_confirmed_dead_cas(self):
        lease = self.acquire()
        self.clock.advance(31)
        with self.assertRaises(LeaseStateError):
            self.store.recover('codex', 'thread-origin-1', lease.token, owner_is_dead=False)
        self.assertEqual(self.store.get('codex', 'thread-origin-1').token, lease.token)
        recovered = self.store.recover('codex', 'thread-origin-1', lease.token, owner_is_dead=True)
        self.assertEqual(recovered.state, 'failed')
        self.assertIsNone(self.store.get('codex', 'thread-origin-1'))
        replacement = self.acquire(meeting_id='mtg-recovery')
        self.assertEqual(replacement.meeting_id, 'mtg-recovery')
        with self.assertRaises(LeaseOwnershipError):
            self.store.recover('codex', 'thread-origin-1', lease.token, owner_is_dead=True)

    def test_stale_recovery_race_has_one_winner(self):
        lease = self.acquire()
        self.clock.advance(31)
        barrier = threading.Barrier(2)
        outcomes = []

        def run():
            barrier.wait()
            store = SessionLeaseStore(self.store.root, clock=self.clock)
            try:
                store.recover('codex', 'thread-origin-1', lease.token, owner_is_dead=True)
                outcomes.append('won')
            except (LeaseStateError, LeaseOwnershipError):
                outcomes.append('lost')

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ['lost', 'won'])
        self.assertIsNone(self.store.get('codex', 'thread-origin-1'))

    def test_same_session_second_acquire_conflicts(self):
        self.acquire()
        with self.assertRaises(LeaseConflictError):
            self.acquire(meeting_id='mtg-other')

    def test_traversal_and_symlink_resistance(self):
        outside = Path(self.temporary.name) / 'outside'
        outside.mkdir()
        victim = outside / 'stolen.json'
        victim.write_text('keep-me', encoding='utf-8')
        digest = lease_digest('codex', 'thread-link')
        link = self.store.root / (digest + '.json')
        link.symlink_to(victim)
        with self.assertRaises(ValueError):
            self.store.acquire(session_ref('thread-link'), 'mtg-a')
        self.assertEqual(victim.read_text(encoding='utf-8'), 'keep-me')
        with self.assertRaises(ValueError):
            AgentSessionRef.from_dict({
                'provider': 'codex',
                'sessionId': '../etc/passwd',
                'workspace': '/Users/Taylor/project',
            })


if __name__ == '__main__':
    unittest.main()
