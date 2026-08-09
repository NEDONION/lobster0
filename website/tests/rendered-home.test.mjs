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

test('publishes production canonicals and a sitemap', async () => {
  const [zh, en, sitemap] = await Promise.all([
    html('index.html'),
    html('en/index.html'),
    html('sitemap-0.xml'),
  ]);
  assert.match(zh, /rel="canonical" href="https:\/\/miniclaw\.vercel\.app\/"/);
  assert.match(en, /rel="canonical" href="https:\/\/miniclaw\.vercel\.app\/en\/"/);
  assert.match(sitemap, /https:\/\/miniclaw\.vercel\.app\/docs\//);
  assert.match(sitemap, /https:\/\/miniclaw\.vercel\.app\/en\/docs\//);
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
  assert.match(zh, /Reflect\.get\(document,[`"']execCommand[`"']\)/);
});

test('shows evidence without overstating live acceptance', async () => {
  const [zh, en] = await Promise.all([html('index.html'), html('en/index.html')]);
  assert.match(zh, /33 条 versioned Channel cases/);
  assert.match(zh, /Implementation PASS 不等于 Live PASS/);
  assert.match(en, /Implementation PASS is not Live PASS/);
  assert.match(zh, /miniclaw-tui-approval-warp\.webp/);
  assert.match(zh, /miniclaw-tui-external-cli-warp\.webp/);
});

test('documents the isolated Vercel project settings', async () => {
  const readme = await readFile(new URL('README.md', root), 'utf8');
  assert.match(readme, /Root Directory.*`website`/i);
  assert.match(readme, /Build Command.*`npm run build`/i);
  assert.match(readme, /Output Directory.*`dist`/i);
});
