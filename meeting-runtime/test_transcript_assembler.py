import unittest

from transcript_assembler import TranscriptAssembler


class TranscriptAssemblerTests(unittest.TestCase):
    def test_selects_input_at_or_before_offset_and_keeps_fragments(self):
        assembler = TranscriptAssembler()
        first = assembler.add_delta({
            'type': 'session.input_transcript.delta', 'delta': 'What does ',
            'start_ms': 100, 'end_ms': 400, 'event_id': 'evt-in-1',
        })
        assembler.add_delta({
            'type': 'session.output_transcript.delta', 'delta': 'Let me check. ',
            'start_ms': 450, 'end_ms': 700, 'event_id': 'evt-out-1',
        })
        assembler.add_delta({
            'type': 'session.input_transcript.delta', 'delta': 'the worker lock do?',
            'start_ms': 800, 'end_ms': 1200, 'event_id': 'evt-in-2',
        })
        self.assertEqual(first.source, 'input')
        self.assertEqual(assembler.text_at_or_before(400), 'What does ')
        self.assertEqual(assembler.relevant_request(400), 'What does')
        self.assertEqual(assembler.relevant_request(1200), 'What does the worker lock do?')
        self.assertEqual(len(assembler.fragments), 3)

    def test_unknown_events_empty_deltas_and_missing_offsets_are_tolerated(self):
        assembler = TranscriptAssembler()
        self.assertIsNone(assembler.add_delta({'type': 'session.future.unknown', 'delta': 'x'}))
        self.assertIsNone(assembler.add_delta({'type': 'session.input_transcript.delta', 'delta': ''}))
        self.assertIsNone(assembler.add_delta({'type': 'response.event', 'event': {}}))
        assembler.add_delta({
            'type': 'session.input_transcript.delta', 'delta': 'Retry that lookup.',
        })
        self.assertEqual(assembler.relevant_request(0), 'Retry that lookup.')
        self.assertEqual(assembler.relevant_request(None), 'Retry that lookup.')

    def test_overlapping_speech_concatenates_in_arrival_order(self):
        assembler = TranscriptAssembler()
        assembler.add_delta({
            'type': 'session.input_transcript.delta', 'delta': 'alpha ',
            'start_ms': 50, 'end_ms': 400,
        })
        assembler.add_delta({
            'type': 'session.input_transcript.delta', 'delta': 'beta',
            'start_ms': 60, 'end_ms': 90,
        })
        self.assertEqual(assembler.relevant_request(100), 'alpha beta')


if __name__ == '__main__':
    unittest.main()
