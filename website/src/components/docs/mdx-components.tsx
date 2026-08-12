import type { MDXComponents } from 'mdx/types';
import defaultMdxComponents from 'fumadocs-ui/mdx';

import { InstallCommand, ReleaseVersion } from './InstallCommand';

export function getMDXComponents(components?: MDXComponents): MDXComponents {
  return {
    ...defaultMdxComponents,
    // Version-bearing snippets come from `siteFacts` so the docs cannot drift
    // out of sync with what the homepage tells people to run.
    InstallCommand,
    ReleaseVersion,
    ...components,
  };
}
