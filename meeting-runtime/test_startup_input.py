import unittest

from startup_input import (
    MESSAGE_LIMIT, TOKEN_LIMIT, clip_tokens, estimate_tokens, handoff_to_session_input,
    truncate_session_input,
)
from test_schemas import context_payload, permissions_payload


class StartupInputTests(unittest.TestCase):
    def test_handoff_orders_developer_dialogue_and_open_questions(self):
        messages = handoff_to_session_input(context_payload(), permissions_payload())
        self.assertEqual(messages[0]['role'], 'developer')
        self.assertEqual(messages[0]['content'][0]['type'], 'input_text')
        self.assertIn('Ship the developer platform', messages[0]['content'][0]['text'])
        self.assertIn('workspace=read-only', messages[0]['content'][0]['text'])
        self.assertEqual(messages[1]['role'], 'user')
        self.assertEqual(messages[2]['role'], 'assistant')
        self.assertTrue(messages[-1]['content'][0]['text'].startswith('Open questions:'))
        self.assertLessEqual(len(messages), MESSAGE_LIMIT)

    def test_truncates_to_message_and_token_limits(self):
        huge = [{'type': 'message', 'role': 'developer',
                 'content': [{'type': 'input_text', 'text': 'keep-me ' + ('x' * 40)}]}]
        huge.extend({'type': 'message', 'role': 'user',
                     'content': [{'type': 'input_text', 'text': f'turn-{index} ' + ('y' * 20)}]}
                    for index in range(200))
        truncated = truncate_session_input(huge)
        self.assertLessEqual(len(truncated), MESSAGE_LIMIT)
        self.assertEqual(truncated[0]['role'], 'developer')
        self.assertIn('keep-me', truncated[0]['content'][0]['text'])
        self.assertLessEqual(sum(estimate_tokens(part['text'])
                                 for item in truncated for part in item['content']), TOKEN_LIMIT)

        oversized = [{'type': 'message', 'role': 'user',
                      'content': [{'type': 'input_text', 'text': 'z' * 50000}]}]
        clipped = truncate_session_input(oversized)
        self.assertEqual(len(clipped), 1)
        self.assertLessEqual(estimate_tokens(clipped[0]['content'][0]['text']), TOKEN_LIMIT)

    def test_commentary_clip_is_500_tokens(self):
        text = clip_tokens('word ' * 4000, 500)
        self.assertLessEqual(estimate_tokens(text), 500)
        self.assertGreater(len(text), 100)


if __name__ == '__main__':
    unittest.main()
