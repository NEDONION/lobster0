import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { siteFacts } from './site';

const DOCS_DIR = join(process.cwd(), 'content/docs');

function mdxFiles(): { name: string; text: string }[] {
  return readdirSync(DOCS_DIR)
    .filter((name) => name.endsWith('.mdx'))
    .map((name) => ({ name, text: readFileSync(join(DOCS_DIR, name), 'utf8') }));
}

/**
 * The install command embeds the release version twice — in the download URL and
 * in the wheel filename — so a hand-pasted copy in the docs turns into a silent
 * 404 the next time a version ships. `<InstallCommand />` renders it from
 * `siteFacts` instead; these guards make sure nobody quietly pastes it back.
 */
describe('docs never hardcode the release artifacts', () => {
  it('has no literal wheel filename or release download URL in MDX', () => {
    const offenders = mdxFiles()
      .filter(({ text }) => /lobster0_agent-[\d.]+-py3|releases\/download\/v[\d.]+/.test(text))
      .map(({ name }) => name);
    expect(offenders).toEqual([]);
  });

  it('builds the install command from the single release version', () => {
    expect(siteFacts.install).toContain(`lobster0_agent-${siteFacts.version}-py3-none-any.whl`);
    expect(siteFacts.install).toContain(`releases/download/v${siteFacts.version}/`);
    expect(siteFacts.installMirrored).toContain(`releases/download/v${siteFacts.version}/`);
    // Braces are load bearing: a bare $W[feishu] is array subscripting in zsh.
    expect(siteFacts.install).toContain('"/tmp/${W}[feishu]"');
    expect(siteFacts.install).not.toContain('$W[');
  });
});
