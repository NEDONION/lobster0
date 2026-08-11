import { describe, expect, it } from 'vitest';

import { marketingCopy, siteFacts, type Locale } from './site';

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

/**
 * Locale copy gets edited by bulk scripts, and a mis-scoped replace can drop one
 * language's string into another language's block without breaking anything —
 * that shipped once (Japanese text landed in the English copy while ja/ko/fr got
 * the English fallback). These two guards catch the whole class mechanically.
 */
describe('locale copy integrity', () => {
  const scripts = { han: /[\u4e00-\u9fff]/, hangul: /[\uac00-\ud7a3]/, kana: /[\u3040-\u30ff]/ };
  const allowedScripts: Record<Locale, (keyof typeof scripts)[]> = {
    'zh-CN': ['han'],
    en: [],
    ja: ['kana', 'han'],
    ko: ['hangul', 'han'],
    fr: [],
  };
  // Keys holding internal identifiers, never rendered as prose.
  const structuralKeys = new Set(['id', 'icon', 'event', 'state', 'detail', 'name', 'github']);
  // Product vocabulary that stays English in every language on purpose.
  const englishByDesign = new Set([
    'Issues',
    'Transport',
    'Delivery',
    'Agent · Policy · Tools · Memory',
    'Python Core / Policy / Memory',
    'transport · delivery · queue',
  ]);

  function eachString(value: unknown, path: string, key: string, visit: (s: string, path: string, key: string) => void): void {
    if (typeof value === 'string') return visit(value, path, key);
    if (Array.isArray(value)) return value.forEach((item, i) => eachString(item, `${path}[${i}]`, key, visit));
    if (value && typeof value === 'object') {
      return Object.entries(value).forEach(([k, v]) => eachString(v, `${path}.${k}`, k, visit));
    }
  }

  it('never mixes one language\'s script into another locale', () => {
    const found: string[] = [];
    for (const [locale, copy] of Object.entries(marketingCopy) as [Locale, unknown][]) {
      eachString(copy, '', '', (text, path) => {
        for (const [name, pattern] of Object.entries(scripts)) {
          const allowed = allowedScripts[locale].includes(name as keyof typeof scripts);
          if (!allowed && pattern.test(text)) found.push(`${locale}${path} has ${name}: "${text}"`);
        }
      });
    }
    expect(found).toEqual([]);
  });

  // ASCII quotes inside CJK prose are a typography error in those languages, and
  // one locale had drifted into mixing two quoting systems at once.
  it('uses each language\'s own quotation marks', () => {
    const found: string[] = [];
    for (const locale of ['zh-CN', 'ja', 'ko'] as const) {
      eachString(marketingCopy[locale], '', '', (text, path, key) => {
        if (structuralKeys.has(key)) return;
        if (/["']/.test(text)) found.push(`${locale}${path} = "${text}"`);
      });
    }
    expect(found).toEqual([]);
  });

  it('leaves no untranslated string in ja and ko', () => {
    const found: string[] = [];
    for (const locale of ['ja', 'ko'] as const) {
      eachString(marketingCopy[locale], '', '', (text, path, key) => {
        if (structuralKeys.has(key) || englishByDesign.has(text)) return;
        const native = locale === 'ja' ? /[\u3040-\u30ff\u4e00-\u9fff]/ : /[\uac00-\ud7a3]/;
        if (!native.test(text)) found.push(`${locale}${path} = "${text}"`);
      });
    }
    expect(found).toEqual([]);
  });
});
