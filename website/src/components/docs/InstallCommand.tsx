import { DynamicCodeBlock } from 'fumadocs-ui/components/dynamic-codeblock';

import { siteFacts } from '@/content/site';
import { codeThemes } from '@/lib/code-theme';

/**
 * Renders the install command from `siteFacts` instead of letting each MDX file
 * paste its own copy. The command carries the release version inside both the
 * download URL and the wheel filename, so a hand-copied block goes stale into a
 * 404 the moment a new version ships — silently, since nothing type-checks prose.
 */
export function InstallCommand({ mirrored = false }: { mirrored?: boolean }) {
  return (
    <DynamicCodeBlock
      code={mirrored ? siteFacts.installMirrored : siteFacts.install}
      lang="bash"
      options={{ themes: codeThemes }}
    />
  );
}

/** The released version, for inline use in docs prose. */
export function ReleaseVersion() {
  return <>{siteFacts.version}</>;
}
