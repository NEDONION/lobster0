import { access, readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const docs = resolve('content/docs');
const slugs = ['index', 'getting-started', 'runtime', 'security', 'channels', 'memory'];

describe('bilingual docs', () => {
  it('pairs every Chinese and English file', async () => {
    for (const slug of slugs) {
      await access(join(docs, `${slug}.mdx`));
      await access(join(docs, `${slug}.en.mdx`));
    }
  });

  it('keeps Live PASS language explicit', async () => {
    expect(await readFile(join(docs, 'index.mdx'), 'utf8')).toContain('Live PASS');
    expect(await readFile(join(docs, 'index.en.mdx'), 'utf8')).toContain('Live PASS');
  });

  it('keeps both sidebars in the same order', async () => {
    const zh = JSON.parse(await readFile(join(docs, 'meta.json'), 'utf8')) as { pages: string[] };
    const en = JSON.parse(await readFile(join(docs, 'meta.en.json'), 'utf8')) as { pages: string[] };
    expect(zh.pages).toEqual(slugs);
    expect(en.pages).toEqual(slugs);
  });
});
