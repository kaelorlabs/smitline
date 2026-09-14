import unittest
from unittest.mock import AsyncMock, MagicMock
from plot_share import extract_plot, plot_html, share_plot_file


class PlotTests(unittest.TestCase):
    def test_untrusted_labels_are_escaped_and_data_preserved(self):
        data = extract_plot('Here is the plot:\n```plot\n{"title":"Sales", "unit":"USD", "labels":["<script>"], "values":[1234.5]}\n```')
        rendered = plot_html(data)
        self.assertNotIn('<script>', rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertIn('1,234.50', rendered)

    def test_rejects_invalid_and_unbounded_data(self):
        for values in ('[NaN]', '[-1]', '[true]', '[]'):
            with self.assertRaises(ValueError):
                extract_plot('```plot\n{"title":"Sales","unit":"USD","labels":["Aug"],"values":' + values + '}\n```')
        self.assertIsNone(extract_plot('Ordinary factual answer'))


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_chat_is_not_toggled_closed_and_missing_file_is_reported(self):
        page = MagicMock()
        page.bring_to_front = AsyncMock()
        page.get_by_text.return_value.is_visible = AsyncMock(return_value=False)
        closed = MagicMock()
        closed.count = AsyncMock(return_value=1)
        hidden = MagicMock()
        hidden.is_visible = AsyncMock(return_value=False)
        recipient = MagicMock()
        recipient.first.is_visible = AsyncMock(return_value=True)
        missing_file = MagicMock()
        missing_file.first.is_visible = AsyncMock(return_value=False)

        def control(role, name, **kwargs):
            if name == 'close the chat panel':
                return closed
            if name == 'Got it':
                return hidden
            if hasattr(name, 'pattern') and name.pattern.startswith('^Send chat to'):
                return recipient
            if hasattr(name, 'pattern') and name.pattern.startswith('^(File|'):
                return missing_file
            self.fail(f'Unexpected control: {name}')

        page.get_by_role.side_effect = control
        result = await share_plot_file(page, '/tmp/plot-test.png', 'Sales')
        self.assertEqual(result['status'], 'saved_locally')
        self.assertEqual(result['error_code'], 'file_transfer_unavailable')
        page.mouse.move.assert_not_called()
