import assert from 'node:assert/strict';
import { access, readFile } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../', import.meta.url);

test('uses one Next.js and Fumadocs application', async () => {
  const pkg = JSON.parse(await readFile(new URL('package.json', root), 'utf8'));

  assert.equal(pkg.scripts.dev, 'next dev');
  assert.equal(pkg.dependencies.next, '16.3.0');
  assert.equal(pkg.dependencies['fumadocs-ui'], '16.14.2');
  await access(new URL('next.config.mjs', root));
  await access(new URL('source.config.ts', root));
  await assert.rejects(access(new URL('astro.config.mjs', root)));
});
