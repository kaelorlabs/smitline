import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from events import ColleagueEvent
from event_store import EventStore, JsonlEventStore


TIMESTAMP = '2026-09-16T17:00:00Z'


def joining_event(meeting_id, event_id):
    return {
        'version': 1,
        'id': event_id,
        'meetingId': meeting_id,
        'timestamp': TIMESTAMP,
        'type': 'meeting.joining',
    }


def append_joining_events(root, meeting_id, prefix, count):
    store = JsonlEventStore(root)
    for index in range(count):
        store.append(joining_event(meeting_id, f'{prefix}-{index}'))


class JsonlEventStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = JsonlEventStore(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def events_path(self, meeting_id='mtg-a'):
        return self.root / meeting_id / 'events.jsonl'

    def test_interface_and_ordered_round_trip_across_restart(self):
        self.assertIsInstance(self.store, EventStore)
        first = self.store.append(joining_event('mtg-a', 'evt-1'))
        self.store.append({
            'version': 1, 'id': 'evt-2', 'meetingId': 'mtg-a', 'timestamp': TIMESTAMP,
            'type': 'meeting.ended', 'reason': 'host_ended',
        })
        restarted = JsonlEventStore(self.root)
        replayed = restarted.replay('mtg-a')
        self.assertEqual([event.id for event in replayed], ['evt-1', 'evt-2'])
        self.assertEqual(replayed[0].to_dict(), first.to_dict())
        self.assertEqual(replayed[1].type, 'meeting.ended')
        self.assertEqual(replayed[1].payload['reason'], 'host_ended')
        self.assertEqual(self.events_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.events_path().parent.stat().st_mode & 0o777, 0o700)

    def test_meetings_are_isolated(self):
        self.store.append(joining_event('mtg-a', 'a-1'))
        self.store.append(joining_event('mtg-b', 'b-1'))
        self.store.append({
            'version': 1, 'id': 'a-2', 'meetingId': 'mtg-a', 'timestamp': TIMESTAMP,
            'type': 'meeting.live',
        })
        self.assertEqual([event.id for event in self.store.replay('mtg-a')], ['a-1', 'a-2'])
        self.assertEqual([event.id for event in self.store.replay('mtg-b')], ['b-1'])
        self.assertEqual(self.store.replay('mtg-missing'), [])
        self.assertTrue((self.root / 'mtg-a' / 'events.jsonl').exists())
        self.assertTrue((self.root / 'mtg-b' / 'events.jsonl').exists())

    def test_malformed_records_and_truncated_tail_do_not_drop_prior_events(self):
        self.store.append(joining_event('mtg-a', 'evt-1'))
        path = self.events_path()
        with path.open('a', encoding='utf-8') as stream:
            stream.write('{"this is": "not an event"}\n')
            stream.write('{not-json\n')
            stream.write('{"version":1,"id":"evt-2","meetingId":"mtg-a","timestamp":"'
                         + TIMESTAMP + '","type":"meeting.live"}\n')
            stream.write('{"version":1,"id":"evt-truncated"')
            stream.flush()
            os.fsync(stream.fileno())
        replayed = self.store.replay('mtg-a')
        self.assertEqual([event.id for event in replayed], ['evt-1', 'evt-2'])
        self.store.append(joining_event('mtg-a', 'evt-3'))
        replayed = JsonlEventStore(self.root).replay('mtg-a')
        self.assertEqual([event.id for event in replayed], ['evt-1', 'evt-2', 'evt-3'])
        lines = path.read_text(encoding='utf-8').splitlines()
        self.assertTrue(all(line.endswith('}') or line.startswith('{') for line in lines))
        parsed_ids = []
        for line in lines:
            try:
                parsed_ids.append(ColleagueEvent.from_dict(json.loads(line)).id)
            except (json.JSONDecodeError, ValueError):
                continue
        self.assertEqual(parsed_ids, ['evt-1', 'evt-2', 'evt-3'])
        self.assertNotIn('evt-truncated', path.read_text(encoding='utf-8'))

    def test_complete_record_missing_final_newline_is_recovered(self):
        self.store.append(joining_event('mtg-a', 'evt-1'))
        path = self.events_path()
        complete = json.dumps(joining_event('mtg-a', 'evt-2'), ensure_ascii=False)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(complete)
            stream.flush()
            os.fsync(stream.fileno())
        self.assertEqual([event.id for event in self.store.replay('mtg-a')], ['evt-1', 'evt-2'])
        self.store.append(joining_event('mtg-a', 'evt-3'))
        self.assertEqual([event.id for event in self.store.replay('mtg-a')],
                         ['evt-1', 'evt-2', 'evt-3'])

    def test_concurrent_thread_appends_do_not_interleave_records(self):
        workers = 8
        count = 20
        barrier = threading.Barrier(workers)

        def run(prefix):
            barrier.wait()
            append_joining_events(str(self.root), 'mtg-a', prefix, count)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run, [f't{index}' for index in range(workers)]))
        replayed = self.store.replay('mtg-a')
        self.assertEqual(len(replayed), workers * count)
        self.assertEqual(len({event.id for event in replayed}), workers * count)
        raw_lines = self.events_path().read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(raw_lines), workers * count)
        for line in raw_lines:
            parsed = json.loads(line)
            self.assertNotIn('\n', line)
            ColleagueEvent.from_dict(parsed)

    def test_concurrent_process_appends_do_not_interleave_records(self):
        workers = 4
        count = 15
        context = multiprocessing.get_context('spawn')
        processes = []
        for index in range(workers):
            process = context.Process(
                target=append_joining_events,
                args=(str(self.root), 'mtg-a', f'p{index}', count),
            )
            process.start()
            processes.append(process)
        for process in processes:
            process.join(timeout=15)
            self.assertEqual(process.exitcode, 0)
        replayed = JsonlEventStore(self.root).replay('mtg-a')
        self.assertEqual(len(replayed), workers * count)
        raw_lines = self.events_path().read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(raw_lines), workers * count)
        for line in raw_lines:
            ColleagueEvent.from_dict(json.loads(line))

    def test_secret_payloads_are_not_persisted(self):
        with self.assertRaises(ValueError):
            self.store.append(joining_event('mtg-a', 'evt-1') | {'apiKey': 'sk-secret'})
        leaked = ColleagueEvent.from_dict(joining_event('mtg-a', 'evt-1'))
        object.__setattr__(leaked, 'payload', dict(leaked.payload) | {'access_token': 'secret'})
        with self.assertRaises(ValueError):
            self.store.append(leaked)
        self.assertFalse(self.events_path().exists())
        self.assertEqual(self.store.replay('mtg-a'), [])


if __name__ == '__main__':
    unittest.main()
