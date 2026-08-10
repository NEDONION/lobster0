import { describe, expect, it } from 'vitest';

import { marketingCopy, siteFacts } from './site';

describe('site content', () => {
  it('keeps repository facts in one source', () => {
    expect(siteFacts.counts).toEqual({
      surfaces: 4,
      tools: 18,
      channelCases: 33,
      automationCases: 15,
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

  it('keeps the production-status disclosure in both locales', () => {
    expect(marketingCopy['zh-CN'].evidence.disclosure).toContain('Live PASS');
    expect(marketingCopy.en.evidence.disclosure).toContain('Live PASS');
  });
});
