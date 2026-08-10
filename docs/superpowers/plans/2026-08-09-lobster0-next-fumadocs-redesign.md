# MiniClaw Next.js + Fumadocs Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Astro/Starlight website in `website/` with a dense three-screen bilingual Next.js homepage and bilingual Fumadocs documentation, then verify and deploy it through Vercel.

**Architecture:** One Next.js 16 App Router application owns marketing and docs routes. Fumadocs supplies MDX, page trees, search, and Docs Layout; a `[lang]` route plus Fumadocs proxy keeps Chinese prefix-free and English under `/en`. Static content stays in Server Components while Claw Trace, capability tabs, and workbench tabs are focused Client Component islands.

**Tech Stack:** Next.js 16.3.0, React 19.2.8, Fumadocs Core/UI 16.14.2, Fumadocs MDX 15.2.2, Tailwind CSS 4.3.3, Motion 13.0.0, TypeScript 6.0.3, Vitest 4.1.10, Testing Library 16.3.2, Playwright 1.62.1, Vercel.

## Global Constraints

- Keep all website source, content, tests, and deployment configuration in repository root `website/`.
- Use Node.js 22.19+ and preserve the repository Python 3.12+ requirement.
- Public routes are `/`, `/en`, `/docs/*`, and `/en/docs/*`; add no marketing feature routes.
- Chinese (`zh-CN`) has no prefix; English uses `/en`.
- Desktop marketing length targets 2.8–3.2 viewports; mobile protects readability and 44px touch targets.
- Facts remain one Python core, four surfaces, 18 tools, 33 Channel cases, 15 Automation cases, four permission modes.
- State that Implementation PASS is not Live PASS; never present planned behavior as implemented.
- Use a light workbench palette: `#F4F6FA`, `#FFFFFF`, `#10131A`, `#667085`, `#5B6CFF`,
  `#73F7C4`, and `#F2B84B`; reserve `#171B24` for terminals and dark evidence.
- Use Instrument Sans, IBM Plex Mono, and system Chinese sans-serif fonts.
- Tabs support ARIA, ArrowLeft/ArrowRight/Home/End, URL hash restore, invalid-hash fallback, and visible focus.
- Reduced-motion mode reveals final states without sequencing or spatial motion.
- Retain both real TUI WebP screenshots; do not fabricate product screenshots.
- Add no CMS, authentication, analytics, database, live GitHub API, blog, pricing, or versioned docs.
- Verify Preview before requesting explicit Production promotion.

---

## Final File Map

```text
website/
├── content/docs/                         # Chinese MDX plus .en variants
├── public/images/                        # Real TUI evidence
├── src/app/[lang]/page.tsx               # Localized marketing page
├── src/app/[lang]/docs/[[...slug]]/      # Localized Fumadocs pages
├── src/app/api/search/route.ts           # Orama search
├── src/app/not-found.tsx
├── src/app/{robots,sitemap}.ts
├── src/app/opengraph-image.tsx
├── src/components/{docs,marketing}/
├── src/content/site.ts                   # Facts and bilingual copy
├── src/lib/{i18n,layout.shared,source}.ts
├── src/styles/globals.css
├── tests/e2e/
├── next.config.mjs
├── src/proxy.ts
├── source.config.ts
└── playwright.config.ts
```

Replace the Astro config and package contract in Task 1; remove the obsolete Astro content, components, and
tests only after the Next replacement passes local build and browser checks in Task 8.

---

### Task 1: Replace the Astro toolchain with a buildable Next.js/Fumadocs foundation

**Files:**
- Create: `website/tests/foundation.test.mjs`
- Create: `website/next.config.mjs`
- Create: `website/postcss.config.mjs`
- Create: `website/eslint.config.mjs`
- Create: `website/vitest.config.ts`
- Create: `website/tests/setup.ts`
- Create: `website/source.config.ts`
- Create: `website/src/app/[lang]/layout.tsx`
- Create: `website/src/app/[lang]/page.tsx`
- Create: `website/src/styles/globals.css`
- Create: `website/src/types/styles.d.ts`
- Modify: `website/package.json`
- Replace mechanically: `website/package-lock.json`
- Modify: `website/tsconfig.json`
- Modify: `website/.gitignore`
- Delete: `website/astro.config.mjs`

**Interfaces:**
- Consumes: Node.js 22.19+ and the current `website/` worktree.
- Produces: Next scripts, `@/*` and `collections/*` aliases, Fumadocs MDX generation, and one buildable locale page.

- [ ] **Step 1: Write the failing foundation contract**

```js
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
```

- [ ] **Step 2: Run the contract and observe the Astro-state failure**

Run: `cd website && node --test tests/foundation.test.mjs`

Expected: FAIL because `scripts.dev` is `astro dev` and Next config is absent.

- [ ] **Step 3: Replace the package contract and lockfile**

Set scripts to `next dev`, `next build --webpack`, `next start`, `eslint .`, `tsc --noEmit`, `vitest run`,
and `playwright test`. Webpack is explicit because Turbopack's PostCSS worker cannot bind its temporary port
inside the local managed execution environment. Pin direct dependencies exactly:

```json
{
  "@fontsource-variable/instrument-sans": "5.3.0",
  "@fontsource/ibm-plex-mono": "5.3.0",
  "@orama/tokenizers": "3.1.18",
  "fumadocs-core": "16.14.2",
  "fumadocs-mdx": "15.2.2",
  "fumadocs-ui": "16.14.2",
  "motion": "13.0.0",
  "next": "16.3.0",
  "react": "19.2.8",
  "react-dom": "19.2.8"
}
```

Pin dev dependencies to `@playwright/test@1.62.1`, `@tailwindcss/postcss@4.3.3`,
`@testing-library/dom@10.4.1`, `@testing-library/jest-dom@7.0.0`,
`@testing-library/react@16.3.2`, `@testing-library/user-event@14.6.3`, `@types/mdx@2.0.14`,
`@types/node@26.2.0`, `@types/react@19.2.18`, `@types/react-dom@19.2.4`,
`@vitejs/plugin-react@6.0.5`, `eslint@9.39.5`, `eslint-config-next@16.3.0`, `jsdom@29.1.1`,
`tailwindcss@4.3.3`, `typescript@6.0.3`, and `vitest@4.1.10`. Run `npm install` to regenerate
the lockfile.

- [ ] **Step 4: Add framework configuration**

```js
// next.config.mjs
import { createMDX } from 'fumadocs-mdx/next';
const withMDX = createMDX();
export default withMDX({ reactStrictMode: true, images: { formats: ['image/avif', 'image/webp'] } });
```

```ts
// source.config.ts
import { defineConfig, defineDocs } from 'fumadocs-mdx/config';
export const docs = defineDocs({ dir: 'content/docs' });
export default defineConfig();
```

```js
// postcss.config.mjs
export default { plugins: { '@tailwindcss/postcss': {} } };
```

Configure ESLint with `eslint-config-next/core-web-vitals` and `eslint-config-next/typescript`.
Configure Vitest with React, jsdom, `tests/setup.ts`, and `@ → src`. Configure TypeScript as strict ES2022
with aliases `@/* → src/*` and `collections/* → .source/*`. `tests/setup.ts` imports
`@testing-library/jest-dom/vitest`, so every DOM matcher used by later tasks is available before the first
Vitest suite runs.

- [ ] **Step 5: Add a minimal localized layout and page**

```tsx
import '@fontsource-variable/instrument-sans';
import '@fontsource/ibm-plex-mono/400.css';
import '@/styles/globals.css';
import type { ReactNode } from 'react';

export function generateStaticParams() {
  return [{ lang: 'zh-CN' }, { lang: 'en' }];
}

export default async function LocaleLayout({ children, params }: {
  children: ReactNode;
  params: Promise<{ lang: string }>;
}) {
  const { lang } = await params;
  return <html lang={lang}><body>{children}</body></html>;
}
```

The minimal page renders `<main><h1>MiniClaw</h1></main>`. Add the approved CSS variables and generated
directories to `.gitignore`; delete `astro.config.mjs`.

- [ ] **Step 6: Verify and commit the foundation**

Run:

```bash
cd website
node --test tests/foundation.test.mjs
npm run typecheck
npm run lint
npm run build
```

Expected: all PASS. Commit with `feat(website): 迁移到 Next.js 与 Fumadocs foundation`.

---

### Task 2: Establish typed facts, bilingual copy, and prefix-free locale routing

**Files:**
- Create: `website/src/content/site.ts`
- Create: `website/src/content/site.test.ts`
- Create: `website/src/lib/i18n.ts`
- Create: `website/src/proxy.ts`
- Create: `website/src/components/marketing/MarketingHome.tsx`
- Create: `website/src/components/marketing/MarketingHeader.tsx`
- Create: `website/src/components/marketing/MarketingFooter.tsx`
- Modify: `website/src/app/[lang]/layout.tsx`
- Modify: `website/src/app/[lang]/page.tsx`
- Modify: `website/src/styles/globals.css`

**Interfaces:**
- Consumes: Task 1 aliases and verified content from existing JSON files.
- Produces: `Locale`, `CapabilityId`, `WorkflowId`, `siteFacts`, `marketingCopy`, `getLocale`, and the three-section server shell.

- [ ] **Step 1: Write the failing content contract**

```ts
import { describe, expect, it } from 'vitest';
import { marketingCopy, siteFacts } from './site';

describe('site content', () => {
  it('keeps repository facts in one source', () => {
    expect(siteFacts.counts).toEqual({
      surfaces: 4, tools: 18, channelCases: 33, automationCases: 15, permissionModes: 4,
    });
    expect(siteFacts.status.implementationPassIsLivePass).toBe(false);
  });

  it('aligns bilingual tabs', () => {
    expect(marketingCopy['zh-CN'].capabilities.map((x) => x.id)).toEqual(
      marketingCopy.en.capabilities.map((x) => x.id),
    );
    expect(marketingCopy['zh-CN'].workflows.map((x) => x.id)).toEqual(
      marketingCopy.en.workflows.map((x) => x.id),
    );
  });
});
```

- [ ] **Step 2: Run `npx vitest run src/content/site.test.ts`**

Expected: FAIL because `site.ts` is absent.

- [ ] **Step 3: Implement exact public content types and data**

```ts
export type Locale = 'zh-CN' | 'en';
export type CapabilityId = 'runtime' | 'channels' | 'safety' | 'memory' | 'automation';
export type WorkflowId = 'approval' | 'external-cli' | 'multi-channel';

export interface CapabilityCopy {
  id: CapabilityId;
  label: string;
  title: string;
  summary: string;
  facts: readonly string[];
}

export interface WorkflowCopy {
  id: WorkflowId;
  label: string;
  title: string;
  summary: string;
  image?: { src: string; alt: string; width: number; height: number };
}
```

Export immutable facts with the current install command, URLs, requirements, trace events, counts, surfaces,
permission modes, and status boundary. Both locales contain five capabilities and three workflows in the
type-defined order.

- [ ] **Step 4: Add prefix-free Chinese i18n routing**

```ts
import { defineI18n } from 'fumadocs-core/i18n';

export const i18n = defineI18n({
  languages: ['zh-CN', 'en'],
  defaultLanguage: 'zh-CN',
  fallbackLanguage: null,
  hideLocale: 'default-locale',
});
```

`getLocale` accepts only `zh-CN` and `en`. `proxy.ts` uses `createI18nMiddleware(i18n)` and excludes API,
Next static/image routes, favicon, and `/images` from its matcher.

- [ ] **Step 5: Build and verify the localized marketing shell**

`MarketingHome` renders exactly three sections with IDs `hero`, `product`, and `workbench`.
`MarketingHeader` links to both anchors, localized docs, alternate language, and GitHub. `MarketingFooter`
links to Docs, GitHub, and Issues. Update `[lang]/page.tsx` to validate locale and render the shell.

Run `npm test -- src/content/site.test.ts && npm run typecheck && npm run build`.

Expected: PASS. Commit with `feat(website): 建立双语三屏 content contract`.

---

### Task 3: Build the dense Hero Runtime, copy interaction, and animated Claw Trace

**Files:**
- Create: `website/src/components/marketing/CommandCopy.tsx`
- Create: `website/src/components/marketing/CommandCopy.test.tsx`
- Create: `website/src/components/marketing/ClawTrace.tsx`
- Create: `website/src/components/marketing/ClawTrace.test.tsx`
- Create: `website/src/components/marketing/EvidenceStrip.tsx`
- Create: `website/src/components/marketing/HeroRuntime.tsx`
- Modify: `website/src/components/marketing/MarketingHome.tsx`
- Modify: `website/src/styles/globals.css`

**Interfaces:**
- Consumes: shared install command, trace events, counts, and localized hero copy.
- Produces: `HeroRuntime`, `CommandCopy`, `ClawTrace`, and `EvidenceStrip`.

- [ ] **Step 1: Write failing copy and reduced-motion tests**

```tsx
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { CommandCopy } from './CommandCopy';

it('copies the complete command and announces success', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.assign(navigator, { clipboard: { writeText } });
  render(<CommandCopy command={'first\nsecond'} label="复制" copiedLabel="已复制" />);
  fireEvent.click(screen.getByRole('button', { name: '复制' }));
  expect(writeText).toHaveBeenCalledWith('first\nsecond');
  expect(await screen.findByText('已复制')).toBeInTheDocument();
});
```

`ClawTrace.test.tsx` asserts all six events, final state under reduced motion, ordered-list semantics, and no
`setInterval`. Run both tests and expect missing-component failures.

- [ ] **Step 2: Implement clipboard success and fallback**

Try Clipboard API first. On absence or rejection, use an off-screen readonly textarea and
`Reflect.get(document, 'execCommand')` with `copy`. Announce through `aria-live="polite"`, restore the label
after two seconds, and keep selectable command text visible on failure.

- [ ] **Step 3: Implement the one-shot Claw Trace**

Render all events in server output. On first visible entry, Motion advances 0→5 over 2.4 seconds and stops.
Pause while the document is hidden. Reduced motion sets event 5 immediately. Text labels accompany colors.

- [ ] **Step 4: Compose and verify the first screen**

Use a 12-column desktop grid: copy/command columns 1–6, Trace columns 7–12, Evidence Strip full width.
Place the Live PASS disclosure beside facts. Use `min-height: 100svh` without hiding overflow.

Run focused tests, typecheck, lint, and build. Commit with
`feat(website): 构建 dense Hero 与动态 Claw Trace`.

---

### Task 4: Implement accessible hash-driven Capability Explorer tabs

**Files:**
- Create: `website/src/components/marketing/HashTabs.tsx`
- Create: `website/src/components/marketing/HashTabs.test.tsx`
- Create: `website/src/components/marketing/CapabilityExplorer.tsx`
- Create: `website/src/components/marketing/capabilities/RuntimePanel.tsx`
- Create: `website/src/components/marketing/capabilities/ChannelsPanel.tsx`
- Create: `website/src/components/marketing/capabilities/SafetyPanel.tsx`
- Create: `website/src/components/marketing/capabilities/MemoryPanel.tsx`
- Create: `website/src/components/marketing/capabilities/AutomationPanel.tsx`
- Modify: `website/src/components/marketing/MarketingHome.tsx`
- Modify: `website/src/styles/globals.css`

**Interfaces:**
- Consumes: `CapabilityCopy[]` and shared facts.
- Produces: reusable `HashTabs` and the five-panel `CapabilityExplorer`.

- [ ] **Step 1: Write failing ARIA, keyboard, and hash tests**

```tsx
const items = [
  { id: 'runtime', label: 'Runtime', panel: <p>Runtime panel</p> },
  { id: 'safety', label: 'Safety', panel: <p>Safety panel</p> },
] as const;

it('restores a known hash and supports keyboard navigation', () => {
  history.replaceState(null, '', '#safety');
  render(<HashTabs ariaLabel="Capabilities" items={items} />);
  const safety = screen.getByRole('tab', { name: 'Safety' });
  expect(safety).toHaveAttribute('aria-selected', 'true');
  safety.focus();
  fireEvent.keyDown(safety, { key: 'Home' });
  expect(screen.getByRole('tab', { name: 'Runtime' })).toHaveFocus();
});
```

Add cases for ArrowLeft/Right, End, unknown hash fallback without URL rewrite, hashchange, stable panel IDs,
and reduced motion. Run the test and expect the missing module failure.

- [ ] **Step 2: Implement `HashTabs`**

Use `tablist`, roving `tabIndex`, stable `${id}-tab`/`${id}-panel` IDs, and `history.replaceState`.
Read the initial hash after hydration; unknown hashes select the first item without rewriting. Subscribe to
`hashchange`. Animate panel entry with Motion and provide an immediate reduced-motion path.

- [ ] **Step 3: Implement five fact-dense panels**

- Runtime: six-node execution path.
- Channels: one AgentRuntime connected to four isolated edge cards.
- Safety: four modes plus Workspace/argv/network/secret readout.
- Memory: Markdown Truth → projection → SQLite inside Owner boundary.
- Automation: disabled-by-default state, authorization gate, 15 cases, and Live PASS disclosure.

Each panel contains one conclusion, one structural visualization, and three to five verified facts.

- [ ] **Step 4: Compose, verify, and commit the second screen**

Use a stable panel height and horizontal mobile tab scroller with 44px triggers. Do not use
`overflow-x: hidden` to conceal layout defects. Run tab/content tests, typecheck, lint, and build.

Commit with `feat(website): 增加 hash-driven Capability Explorer`.

---

### Task 5: Implement the real-evidence Workbench and third-screen close

**Files:**
- Create: `website/src/components/marketing/Workbench.tsx`
- Create: `website/src/components/marketing/Workbench.test.tsx`
- Create: `website/src/components/marketing/MultiChannelDiagram.tsx`
- Create: `website/src/components/marketing/QuickStartClose.tsx`
- Modify: `website/src/components/marketing/MarketingHome.tsx`
- Modify: `website/src/styles/globals.css`
- Retain: `website/public/images/miniclaw-tui-approval-warp.webp`
- Retain: `website/public/images/miniclaw-tui-external-cli-warp.webp`

**Interfaces:**
- Consumes: `WorkflowCopy[]`, real WebP assets, localized close copy, and shared install command.
- Produces: `Workbench` with three hash-addressable tabs and `QuickStartClose`.

- [ ] **Step 1: Write the failing real-evidence contract**

```tsx
import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { marketingCopy } from '@/content/site';
import { Workbench } from './Workbench';

it('uses two real screenshots and one multi-channel diagram', () => {
  render(<Workbench locale="zh-CN" workflows={marketingCopy['zh-CN'].workflows} />);
  expect(screen.getByRole('tab', { name: /SAFE/ })).toBeInTheDocument();
  expect(screen.getByRole('img', { name: /审批/ })).toHaveAttribute(
    'src', expect.stringContaining('approval'),
  );
  expect(screen.getByRole('tab', { name: /多入口/ })).toBeInTheDocument();
});
```

Run: `cd website && npx vitest run src/components/marketing/Workbench.test.tsx`

Expected: FAIL because `Workbench.tsx` is absent.

- [ ] **Step 2: Implement the three panels**

Reuse `HashTabs`. Approval and External CLI use `next/image` at intrinsic dimensions `2784×1824` and
`2696×1736`, responsive `sizes`, localized captions, and a compact explanatory terminal overlay. The overlay
may highlight execution order but must not change screenshot pixels. Multi-channel renders a labeled diagram
of shared AgentRuntime and isolated Transport, Delivery, queue, and failure state.

- [ ] **Step 3: Add compact Quick Start and close**

Render one command block plus Docs and GitHub CTAs inside the Workbench grid rather than a fourth section.
Workbench plus close targets approximately `90svh` at 1440×900 when content fits.

- [ ] **Step 4: Verify and commit the third screen**

Run the Workbench test, typecheck, lint, and production build. Confirm both WebPs are used with explicit
dimensions. Commit with `feat(website): 构建真实证据 Workbench 与 Quick Start`.

---

### Task 6: Integrate bilingual Fumadocs content, navigation, and Orama search

**Files:**
- Create: `website/content/docs/meta.json`
- Create: `website/content/docs/meta.en.json`
- Create: `website/content/docs/{index,getting-started,runtime,security,channels,memory}.mdx`
- Create: `website/content/docs/{index,getting-started,runtime,security,channels,memory}.en.mdx`
- Create: `website/src/lib/source.ts`
- Create: `website/src/lib/layout.shared.tsx`
- Create: `website/src/components/docs/mdx-components.tsx`
- Create: `website/src/app/[lang]/docs/layout.tsx`
- Create: `website/src/app/[lang]/docs/[[...slug]]/page.tsx`
- Create: `website/src/app/api/search/route.ts`
- Create: `website/src/content/docs.test.ts`
- Modify: `website/src/app/[lang]/layout.tsx`
- Modify: `website/src/styles/globals.css`

**Interfaces:**
- Consumes: Fumadocs config, `i18n`, current Starlight MDX bodies, and verified public links.
- Produces: `source`, `baseOptions(locale)`, MDX map, localized docs pages, static params, metadata, and search.

- [ ] **Step 1: Write the failing bilingual docs contract**

```ts
import { access, readFile } from 'node:fs/promises';
import { describe, expect, it } from 'vitest';

const docs = new URL('../../content/docs/', import.meta.url);
const slugs = ['index', 'getting-started', 'runtime', 'security', 'channels', 'memory'];

describe('bilingual docs', () => {
  it('pairs every Chinese and English file', async () => {
    for (const slug of slugs) {
      await access(new URL(`${slug}.mdx`, docs));
      await access(new URL(`${slug}.en.mdx`, docs));
    }
  });

  it('keeps Live PASS language explicit', async () => {
    expect(await readFile(new URL('index.mdx', docs), 'utf8')).toContain('Live PASS');
    expect(await readFile(new URL('index.en.mdx', docs), 'utf8')).toContain('Live PASS');
  });
});
```

Run the test and expect failure because `content/docs` is absent.

- [ ] **Step 2: Migrate all six bilingual documents**

Move bodies from `src/content/docs/docs/*.mdx` into default Chinese files and from
`src/content/docs/en/docs/*.mdx` into `.en.mdx` files without changing product claims. Use Fumadocs
frontmatter `title` and `description`; replace Starlight imports with Fumadocs Tabs/Tab and callouts.

Use exact page order in both meta files:

```json
{ "title": "用户指南", "pages": ["index", "getting-started", "runtime", "security", "channels", "memory"] }
```

English changes only the title to `User guide`.

- [ ] **Step 3: Implement loader, translations, and MDX components**

```ts
import { docs } from 'collections/server';
import { loader } from 'fumadocs-core/source';
import { i18n } from './i18n';

export const source = loader({
  baseUrl: '/docs',
  i18n,
  source: docs.toFumadocsSource(),
});
```

`layout.shared.tsx` extends official Fumadocs UI translations for `zh-CN` and `en`, maps MiniClaw to the
localized homepage, and exposes GitHub as the external nav action. `mdx-components.tsx` merges
`fumadocs-ui/mdx` defaults with Fumadocs Tabs and Tab.

- [ ] **Step 4: Implement layout, pages, static params, and search**

Import `tailwindcss`, `fumadocs-ui/css/neutral.css`, and `fumadocs-ui/css/preset.css` at the top of
`globals.css`. Wrap every localized route in the official provider:

```tsx
import { i18nProvider } from 'fumadocs-ui/i18n';
import { RootProvider } from 'fumadocs-ui/provider/next';
import { translations } from '@/lib/layout.shared';

<RootProvider i18n={i18nProvider(translations, lang)}>{children}</RootProvider>
```

The docs layout validates `params.lang`, renders standard `DocsLayout`, and passes the localized page tree.
The catch-all page uses `source.getPage(slug, locale)`, calls `notFound()` if absent, renders `DocsPage`,
`DocsTitle`, `DocsDescription`, and `DocsBody`, and generates metadata from frontmatter. Generate all locale
and slug params through `source.generateParams()`. Implement `/api/search` with the Fumadocs 16.14.2
Orama server and a Mandarin tokenizer:

```ts
import { createTokenizer } from '@orama/tokenizers/mandarin';
import { createFromSource } from 'fumadocs-core/search/server';
import { source } from '@/lib/source';

export const { GET } = createFromSource(source, {
  localeMap: {
    'zh-CN': {
      components: { tokenizer: createTokenizer() },
      search: { threshold: 0, tolerance: 0 },
    },
    en: { language: 'english' },
  },
});
```

- [ ] **Step 5: Verify and commit docs**

Run docs tests, typecheck, lint, and build. Expected build includes six Chinese and six English docs pages
and `/api/search`. Commit with `docs(website): 迁移双语 Fumadocs 与 Orama search`.

---

### Task 7: Add metadata, OG image, robots, sitemap, and branded 404

**Files:**
- Create: `website/src/app/sitemap.ts`
- Create: `website/src/app/robots.ts`
- Create: `website/src/app/opengraph-image.tsx`
- Create: `website/src/app/not-found.tsx`
- Create: `website/src/content/metadata.test.ts`
- Modify: `website/src/app/[lang]/layout.tsx`
- Modify: `website/src/app/[lang]/page.tsx`

**Interfaces:**
- Consumes: `https://miniclaw.vercel.app`, locales, docs slugs, and localized metadata copy.
- Produces: Next Metadata, localized sitemap alternates, robots policy, 1200×630 OG image, and 404 links.

- [ ] **Step 1: Write failing metadata route tests**

```ts
import { describe, expect, it } from 'vitest';
import robots from '@/app/robots';
import sitemap from '@/app/sitemap';

describe('public metadata', () => {
  it('publishes localized homes and docs', () => {
    const urls = sitemap().map((entry) => entry.url);
    expect(urls).toContain('https://miniclaw.vercel.app/');
    expect(urls).toContain('https://miniclaw.vercel.app/en');
    expect(urls).toContain('https://miniclaw.vercel.app/docs');
    expect(urls).toContain('https://miniclaw.vercel.app/en/docs');
  });

  it('advertises the sitemap', () => {
    expect(robots().sitemap).toBe('https://miniclaw.vercel.app/sitemap.xml');
  });
});
```

Run the test and expect missing route-module failures.

- [ ] **Step 2: Implement localized metadata and crawler routes**

Set `metadataBase`, title template, icons, OG/Twitter defaults, localized canonical, and hreflang alternates.
Sitemap emits all 14 content routes with Chinese/English alternate pairs. Robots allows `/`, disallows
`/api/`, and declares the sitemap.

- [ ] **Step 3: Implement brand OG and 404**

Use `ImageResponse` at `1200×630` with the approved palette, wordmark, headline, six-step Trace, and verified
`4 surfaces / 18 tools / 33 cases` evidence. Do not use a gradient blob. The 404 links to home, docs, GitHub.

- [ ] **Step 4: Verify and commit metadata**

Run metadata tests, typecheck, lint, and build. Confirm Next lists sitemap, robots, OG, and not-found routes.
Commit with `feat(website): 完成 metadata、OG 与 branded 404`.

---

### Task 8: Add E2E gates, remove Astro artifacts, and update operations docs

**Files:**
- Create: `website/playwright.config.ts`
- Create: `website/tests/e2e/marketing.spec.ts`
- Create: `website/tests/e2e/docs.spec.ts`
- Create: `website/tests/e2e/links.spec.ts`
- Modify: `website/README.md`
- Delete: `website/src/**/*.astro`
- Delete: `website/src/content.config.ts`
- Delete: `website/src/content/docs/**`
- Delete: `website/src/styles/docs.css`
- Delete: `website/tests/content-contract.test.mjs`
- Delete: `website/tests/rendered-docs.test.mjs`
- Delete: `website/tests/rendered-home.test.mjs`
- Delete generated output: `website/.astro/`, `website/dist/`

**Interfaces:**
- Consumes: completed Next/Fumadocs routes and local production build.
- Produces: deterministic Playwright coverage, one framework tree, and current Vercel instructions.

- [ ] **Step 1: Add Playwright configuration**

```ts
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  webServer: {
    command: 'npm run dev -- --hostname 127.0.0.1',
    url: 'http://127.0.0.1:3000',
    reuseExistingServer: !process.env.CI,
  },
  use: { baseURL: 'http://127.0.0.1:3000', trace: 'retain-on-failure' },
  projects: [
    { name: 'desktop-chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile-chromium', use: { ...devices['Pixel 7'] } },
  ],
});
```

- [ ] **Step 2: Write and run complete browser journeys**

`marketing.spec.ts` asserts three sections, five capability tabs, three workbench tabs, hash restoration,
copy feedback, language switch, reduced-motion final state, ≤3.2 desktop viewport heights at 1440×900, no
mobile overflow, and no page errors. `docs.spec.ts` asserts both locales, search, sidebar/mobile menu, and Live
PASS disclosure. `links.spec.ts` crawls all same-origin links and fragments and checks expected status.

Run: `npx playwright install chromium && npm run test:e2e`.

Expected: both desktop and mobile projects PASS.

- [ ] **Step 3: Remove only the obsolete Astro/Starlight files**

Delete the exact old files listed above after browser tests pass. Retain favicon, original PNGs, optimized
WebPs, Next source, Fumadocs content, and new tests. Search `website/` for `astro|starlight`; dependencies,
configs, imports, scripts, and active source must contain no matches.

- [ ] **Step 4: Rewrite the website README**

Document code location `website/`, local commands, all four route roots, Vercel Root Directory `website`,
Framework Preset Next.js, Build Command `npm run build`, Install Command `npm ci`, framework-managed `.next`
output, Production URL, and Preview-before-Production rule.

- [ ] **Step 5: Run full local and repository-adjacent gates**

```bash
cd website
npm ci
npm run typecheck
npm run lint
npm test
npm run build
npm run test:e2e
cd ..
uv run ruff check .
uv run python scripts/validate_docs.py
git diff --check
git status --short
```

Expected: all checks PASS and status includes only intended migration changes. Commit with
`refactor(website): 完成 Next.js/Fumadocs migration 与 E2E gate`.

---

### Task 9: Deploy and verify Preview, then promote only with explicit approval

**Files:**
- No source changes expected.
- Read: `website/README.md`
- Read if created: `website/.vercel/project.json`

**Interfaces:**
- Consumes: clean `codex/miniclaw-website`, Vercel project `prj_LH9mqjqnxCpZv3jhI7qu9ZIZ8m1Q`, team `team_g6lK7VMRTBPfx0t2HjGgRPkS`.
- Produces: READY Preview, remote build/browser evidence, and explicitly approved Production deployment.

- [ ] **Step 1: Record the exact candidate**

Run `git status --short` and `git rev-parse --short HEAD`.

Expected: clean worktree and one recorded SHA.

- [ ] **Step 2: Create an isolated Vercel Preview**

Deploy `website/` to the existing project and team with target `preview`. Do not pass `production` or modify
the current `miniclaw.vercel.app` alias.

Expected: a unique `.vercel.app` deployment reaches READY.

- [ ] **Step 3: Verify the remote build and all surfaces**

Inspect logs for `npm ci`, Next build, route generation, and zero errors. In a real browser check `/`, `/en`,
`/docs`, `/en/docs`, sitemap, robots, OG, three-screen height, all tabs, copy, search, reduced motion, mobile
overflow, console, resources, same-origin links, and fragments.

- [ ] **Step 4: Request exact Production approval**

After reporting the Preview URL and candidate SHA, ask one exact approval question containing the literal
short SHA printed in Step 1, the words `Vercel Production`, and the target `miniclaw.vercel.app`. For example,
if Step 1 printed `a1b2c3d`, ask: `允许将 commit a1b2c3d 部署到 Vercel Production，覆盖
miniclaw.vercel.app？`

Do not promote without an affirmative response referring to Production.

- [ ] **Step 5: Promote the same verified candidate and recheck Production**

Deploy the same file tree and SHA with target `production`. Confirm READY, alias
`https://miniclaw.vercel.app`, canonical URLs, and the complete route/browser gate.

Expected: Production serves the verified Next.js/Fumadocs build and no required work remains.

---

## Plan Self-Review Coverage

- Framework, routes, locales: Tasks 1–2 and 6.
- Three-screen information architecture: Tasks 3–5.
- Approved visual and motion system: Tasks 3–5 and 8.
- Component and content boundaries: Tasks 2–5.
- Fumadocs, MDX, search, and language pairing: Task 6.
- SEO, performance, accessibility, and errors: Tasks 3–8.
- Migration cleanup, tests, and documentation: Task 8.
- Preview, explicit Production gate, and final verification: Task 9.
