import { describe, expect, it } from 'vitest';

import { marketingCopy, siteFacts } from './site';

describe('site content', () => {
  it('keeps repository facts in one source', () => {
    expect(siteFacts.counts).toEqual({
      surfaces: 4,
      tools: 18,
      permissionModes: 4,
    });
    expect(siteFacts.status.implementationPassIsLivePass).toBe(false);
    expect(siteFacts.status.automationDefault).toBe(false);
  });

  it('aligns bilingual capability and workflow tabs', () => {
    expect(marketingCopy['zh-CN'].capabilities.map((item) => item.id)).toEqual([
      'runtime',
      'channels',
      'safety',
      'memory',
      'automation',
    ]);
    expect(marketingCopy.en.capabilities.map((item) => item.id)).toEqual(
      marketingCopy['zh-CN'].capabilities.map((item) => item.id),
    );
    expect(marketingCopy.en.workflows.map((item) => item.id)).toEqual(
      marketingCopy['zh-CN'].workflows.map((item) => item.id),
    );
  });

  // These were internal QA metrics that said nothing to a visitor, and they
  // survived one removal pass by hiding in a second component. Guard every
  // locale so they cannot drift back in.
  it('keeps internal case-count jargon out of every locale', () => {
    const banned = [/versioned cases/i, /Live PASS/i, /Implementation\s*[≠!]=?\s*Live/i];
    for (const [locale, copy] of Object.entries(marketingCopy)) {
      const text = JSON.stringify(copy);
      for (const pattern of banned) {
        expect(`${locale}: ${text.match(pattern)?.[0] ?? 'clean'}`).toBe(`${locale}: clean`);
      }
    }
  });
});
