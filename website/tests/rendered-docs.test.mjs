import assert from 'node:assert/strict';
import { access, readFile } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../dist/', import.meta.url);
const routes = [
  'docs',
  'docs/getting-started',
  'docs/runtime',
  'docs/security',
  'docs/channels',
  'docs/memory',
];

test('builds matching Chinese and English documentation routes', async () => {
  for (const route of routes) {
    await access(new URL(`${route}/index.html`, root));
    await access(new URL(`en/${route}/index.html`, root));
  }
});

test('keeps live-status language explicit in both documentation sets', async () => {
  const zh = await readFile(new URL('docs/index.html', root), 'utf8');
  const en = await readFile(new URL('en/docs/index.html', root), 'utf8');
  assert.match(zh, /真实平台 Live Gate/);
  assert.match(en, /real-platform Live Gate/);
});
