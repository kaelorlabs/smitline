import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const here = new URL('./', import.meta.url);
const read = name => readFile(new URL(name, here), 'utf8');

// The phone header (max-width 480px) only fits in 375px because Account
// shrinks to its icon. That needs the label in its own element on every page.
test('every console page wraps the Account label so phones can show just the icon', async () => {
  for (const page of ['meetings.html', 'calls.html', 'contacts.html', 'setup.html']) {
    const html = await read(page);
    assert.match(html, /<a class="account-link"[^>]*>.*<span class="account-name">Account<\/span><\/a>/, page);
  }
});

test('the phone rules hide the Account label for sight only and keep the nav off the page width', async () => {
  const css = await read('calls.css');
  const start = css.indexOf('@media (max-width: 480px)');
  assert.notEqual(start, -1, 'calls.css has a phone block');
  const phone = css.slice(start);
  assert.match(phone, /\.account-name \{[^}]*position: absolute;[^}]*clip: rect\(0 0 0 0\)/);
  assert.doesNotMatch(phone, /\.account-name \{[^}]*display: none/);
  assert.match(phone, /\.topnav \{[^}]*min-width: 0;[^}]*overflow-x: auto/);
});
