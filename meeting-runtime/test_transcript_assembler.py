import unittest

from startup_input import estimate_tokens
from transcript_assembler import REQUEST_TOKEN_LIMIT, TranscriptAssembler


def input_delta(text, start_ms=None, end_ms=None, event_id=None, **extra):
    event = {'type': 'session.input_transcript.delta', 'delta': text}
    if start_ms is not None:
        event['start_ms'] = start_ms
    if end_ms is not None:
        event['end_ms'] = end_ms
    if event_id is not None:
        event['event_id'] = event_id
    event.update(extra)
    return event


def output_delta(text, start_ms=None, end_ms=None, event_id=None):
    event = {'type': 'session.output_transcript.delta', 'delta': text}
    if start_ms is not None:
        event['start_ms'] = start_ms
    if end_ms is not None:
        event['end_ms'] = end_ms
    if event_id is not None:
        event['event_id'] = event_id
    return event


class TranscriptAssemblerTests(unittest.TestCase):
    def test_latest_utterance_ignores_prior_turns_and_agent_speech(self):
        assembler = TranscriptAssembler()
        assembler.add_delta(input_delta('Let us start with billing.', 0, 900, 't1'))
        assembler.add_delta(output_delta('Sure, I am listening.', 1000, 1600, 'a1'))
        assembler.add_delta(input_delta('What about the invoice total?', 2000, 2800, 't2'))
        assembler.add_delta(output_delta('I will check.', 2900, 3300, 'a2'))
        assembler.add_delta(input_delta('What does ', 4000, 4200, 't3a'))
        assembler.add_delta(input_delta('the worker lock do?', 4250, 4600, 't3b'))
        self.assertEqual(assembler.relevant_request(900), 'Let us start with billing.')
        self.assertEqual(assembler.relevant_request(2800), 'What about the invoice total?')
        self.assertEqual(assembler.relevant_request(4600), 'What does the worker lock do?')
        self.assertIn('invoice total', assembler.text_at_or_before(4600))
        self.assertEqual(len(assembler.fragments), 6)

    def test_backchannel_falls_back_to_preceding_substantive_request(self):
        assembler = TranscriptAssembler()
        assembler.add_delta(input_delta('Inspect the lock file.', 100, 800, 'q1'))
        assembler.add_delta(output_delta('Working.', 900, 1100, 'a1'))
        assembler.add_delta(input_delta('yeah', 3000, 3100, 'bc1'))
        self.assertEqual(assembler.relevant_request(3100), 'Inspect the lock file.')

    def test_duplicate_deltas_are_idempotent_by_event_identity(self):
        assembler = TranscriptAssembler()
        first = assembler.add_delta(input_delta('Retry that lookup.', 10, 40, 'dup-1'))
        again = assembler.add_delta(input_delta('Retry that lookup.', 10, 40, 'dup-1'))
        assembler.add_delta(input_delta('Retry that lookup.', 10, 40, 'dup-1'))
        self.assertIsNotNone(first)
        self.assertIsNone(again)
        self.assertEqual(len(assembler.fragments), 1)
        self.assertEqual(assembler.relevant_request(40), 'Retry that lookup.')

    def test_missing_offsets_do_not_return_the_entire_meeting(self):
        assembler = TranscriptAssembler()
        assembler.add_delta(input_delta('Older topic one. '))
        assembler.add_delta(output_delta('Acknowledged. '))
        assembler.add_delta(input_delta('What does the worker lock do?'))
        self.assertEqual(assembler.relevant_request(0), 'What does the worker lock do?')
        self.assertEqual(assembler.relevant_request(None), 'What does the worker lock do?')

        unbounded = TranscriptAssembler()
        for index in range(40):
            unbounded.add_delta(input_delta('prior turn %s. ' % index, event_id='old-%s' % index))
        unbounded.add_delta(input_delta('Only the latest question remains?', event_id='latest'))
        request = unbounded.relevant_request(None)
        self.assertIn('Only the latest question remains?', request)
        self.assertNotIn('prior turn 0', request)
        self.assertLessEqual(estimate_tokens(request), REQUEST_TOKEN_LIMIT)

    def test_overlapping_speech_stays_one_utterance(self):
        assembler = TranscriptAssembler()
        assembler.add_delta(input_delta('alpha ', 50, 400, 'o1'))
        assembler.add_delta(input_delta('beta', 60, 90, 'o2'))
        self.assertEqual(assembler.relevant_request(100), 'alpha beta')

    def test_timestamp_gap_starts_a_new_request(self):
        assembler = TranscriptAssembler()
        assembler.add_delta(input_delta('First question about billing.', 100, 500, 'g1'))
        assembler.add_delta(input_delta('What does the worker lock do?', 4000, 4800, 'g2'))
        self.assertEqual(assembler.relevant_request(4800), 'What does the worker lock do?')

    def test_unknown_events_and_empty_deltas_are_ignored(self):
        assembler = TranscriptAssembler()
        self.assertIsNone(assembler.add_delta({'type': 'session.future.unknown', 'delta': 'x'}))
        self.assertIsNone(assembler.add_delta({'type': 'session.input_transcript.delta', 'delta': ''}))
        self.assertIsNone(assembler.add_delta({'type': 'response.event', 'event': {}}))
        assembler.add_delta(input_delta('Retry that lookup.', event_id='ok'))
        self.assertEqual(assembler.relevant_request(0), 'Retry that lookup.')

    def test_fragmented_request_inserts_missing_space(self):
        assembler = TranscriptAssembler()
        assembler.add_delta(input_delta('What does', 10, 20, 's1'))
        assembler.add_delta(input_delta('the worker lock do?', 30, 80, 's2'))
        self.assertEqual(assembler.relevant_request(80), 'What does the worker lock do?')


if __name__ == '__main__':
    unittest.main()
