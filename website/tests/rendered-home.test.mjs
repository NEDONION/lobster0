import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../', import.meta.url);
const html = (path) => readFile(new URL(`dist/${path}`, root), 'utf8');

test('renders the localized home routes with reciprocal language links', async () => {
  const [zh, en] = await Promise.all([html('index.html'), html('en/index.html')]);
  assert.match(zh, /小而完整，真正能行动/);
  assert.match(en, /Small by design\. Ready to act\./);
  assert.match(zh, /hreflang="en"/);
  assert.match(en, /hreflang="zh-CN"/);
});

test('renders the real Claw Trace states and install command', async () => {
  const zh = await html('index.html');
  for (const step of [
    'MESSAGE_RECEIVED',
    'POLICY_CHECK',
    'APPROVAL',
    'TOOL_EXECUTION',
    'RESULT_DELIVERED',
  ]) {
    assert.match(zh, new RegExp(step));
  }
  assert.match(zh, /git clone https:\/\/github\.com\/NEDONION\/miniclaw\.git/);
  assert.match(zh, /aria-live="polite"/);
});
