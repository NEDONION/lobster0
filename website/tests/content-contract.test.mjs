import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../', import.meta.url);
const readJson = async (path) => JSON.parse(await readFile(new URL(path, root), 'utf8'));

test('publishes one shared set of project facts', async () => {
  const facts = await readJson('src/data/project-facts.json');
  assert.equal(facts.requirements.python, '3.12+');
  assert.equal(facts.requirements.node, '22.19+');
  assert.equal(facts.counts.tools, 18);
  assert.equal(facts.counts.channelCases, 33);
  assert.equal(facts.counts.automationCases, 15);
  assert.match(facts.install, /^git clone /);
});

test('keeps Chinese and English homepage structures aligned', async () => {
  const content = await readJson('src/data/site-content.json');
  assert.deepEqual(Object.keys(content.zh), Object.keys(content.en));
  assert.equal(content.zh.meta.lang, 'zh-CN');
  assert.equal(content.en.meta.lang, 'en');
  assert.equal(content.zh.trace.steps.length, content.en.trace.steps.length);
});
