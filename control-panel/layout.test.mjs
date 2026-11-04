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

// The Relay look: the arch beside the name on every page (Smitline Cloud's logo), never the old
// three-bar mark, and the same drawing on the pages that cannot load it (setup, connector).
test('every console page shows the Relay arch beside the name', async () => {
  for (const page of ['index.html', 'meetings.html', 'calls.html', 'contacts.html', 'setup.html']) {
    const html = await read(page);
    assert.match(html, /<a class="brand"[^>]*>\s*<img class="brand-mark" src="\/relay-arch-mark\.svg" alt=""[^>]*>\s*<span[^>]*>Smitline<\/span>/, page);
    assert.doesNotMatch(html, /<i><\/i><i><\/i><i><\/i>/, page);
  }
  const { BRAND } = await import('../packages/cli/src/setup.mjs');
  const drawing = (svg) => svg.replace(/^<svg[^>]*>/, '').replace(/<\/svg>[\s\S]*$/, '').trim();
  assert.equal(drawing(BRAND.slice(BRAND.indexOf('<svg'))), drawing(await read('relay-arch-mark.svg')));
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
