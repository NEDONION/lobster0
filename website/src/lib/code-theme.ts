/**
 * Shared by the MDX build pipeline (fenced code blocks, via `source.config.ts`)
 * and by `DynamicCodeBlock` (code injected from TypeScript). They highlight
 * through different code paths, so a theme set in only one of them renders two
 * visibly different code blocks on the same page.
 *
 * Dark on purpose: the site is light-themed, but a command should look identical
 * in the hero terminal and in the docs.
 */
export const CODE_THEME = 'github-dark-default';

export const codeThemes = { dark: CODE_THEME, light: CODE_THEME } as const;
