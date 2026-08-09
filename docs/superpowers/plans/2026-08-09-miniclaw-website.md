# MiniClaw Website Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a distinctive bilingual Astro marketing site and curated Starlight documentation for MiniClaw.

**Architecture:** A static Astro site lives in `website/` and imports one bilingual content document plus one language-neutral project facts document. Two marketing routes reuse the same Astro component tree, while Starlight owns the `/docs/` and `/en/docs/` routes. Vercel builds only `website/` and serves the generated `dist/` directory.

**Tech Stack:** Astro 7.2.0, Starlight 0.41.7, TypeScript 6.0.3, Node built-in test runner, custom CSS, Vercel static deployment.

## Global Constraints

- Chinese is the unprefixed root language; complete English routes use `/en/`.
- Website source lives only in `website/`; Python Core, TUI, and Eval behavior are out of scope.
- Marketing copy describes implemented behavior only and distinguishes Implementation PASS from Live PASS.
- The homepage uses real MiniClaw screenshots and real execution-state vocabulary.
- Do not add React, Tailwind, a generic UI component library, runtime analytics, or a backend.
- Respect keyboard focus, WCAG AA contrast, semantic landmarks, and `prefers-reduced-motion`.
- Vercel Root Directory is `website`; build command is `npm run build`; output is `dist`.
- Six pre-existing Python failures caused by stale 32/101 count assertions remain out of scope.

---

### Task 1: Establish the website content contract and Astro skeleton

**Files:**
- Create: `website/tests/content-contract.test.mjs`
- Create: `website/package.json`
- Create: `website/astro.config.mjs`
- Create: `website/tsconfig.json`
- Create: `website/src/content.config.ts`
- Create: `website/src/data/project-facts.json`
- Create: `website/src/data/site-content.json`
- Create: `website/src/types/site.ts`
- Create: `website/public/favicon.svg`
- Create: `website/.gitignore`

**Interfaces:**
- Produces: `project-facts.json` with `install`, `requirements`, `counts`, `surfaces`, and `links` keys.
- Produces: `site-content.json` with structurally matching `zh` and `en` locale objects.
- Produces: Astro/Starlight build commands consumed by all later tasks.

- [ ] **Step 1: Write the failing content contract test**

```js
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
```

- [ ] **Step 2: Run the test and verify the missing data files fail it**

Run: `node --test website/tests/content-contract.test.mjs`

Expected: FAIL with `ENOENT` for `project-facts.json`.

- [ ] **Step 3: Add the package manifest and static Astro/Starlight configuration**

```json
{
  "name": "miniclaw-website",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "astro dev",
    "build": "astro build",
    "check": "astro check",
    "test": "node --test tests/*.test.mjs",
    "preview": "astro preview"
  },
  "dependencies": {
    "@astrojs/starlight": "0.41.7",
    "@fontsource-variable/archivo": "5.3.0",
    "@fontsource-variable/newsreader": "5.3.0",
    "@fontsource/ibm-plex-mono": "5.3.0",
    "astro": "7.2.0"
  },
  "devDependencies": {
    "@astrojs/check": "0.9.10",
    "typescript": "6.0.3"
  }
}
```

Configure Starlight with a `root` `zh-CN` locale, an `en` locale, bilingual title values,
GitHub social link, curated sidebar entries, and `./src/styles/docs.css` as custom CSS.

- [ ] **Step 4: Add the shared facts, matching bilingual content, types, and favicon**

Use the values in `docs/superpowers/specs/2026-08-09-miniclaw-website-design.md` exactly.
The install value is the complete clone, sync, TUI build, init, doctor, and launch sequence from the current README.

- [ ] **Step 5: Install dependencies and run the contract test**

Run: `npm install`

Run: `npm test`

Expected: 2 tests PASS.

- [ ] **Step 6: Commit the content contract and skeleton**

```bash
git add website
git commit -m "feat(website): 建立 Astro 双语内容契约"
```

### Task 2: Build the shared bilingual marketing homepage

**Files:**
- Create: `website/tests/rendered-home.test.mjs`
- Create: `website/src/layouts/BaseLayout.astro`
- Create: `website/src/components/LogoMark.astro`
- Create: `website/src/components/SiteHeader.astro`
- Create: `website/src/components/CommandBlock.astro`
- Create: `website/src/components/ClawTrace.astro`
- Create: `website/src/components/HomePage.astro`
- Create: `website/src/pages/index.astro`
- Create: `website/src/pages/en/index.astro`
- Create: `website/src/styles/global.css`

**Interfaces:**
- Consumes: `site-content.json`, `project-facts.json`, and `Locale = 'zh' | 'en'`.
- Produces: `/index.html` and `/en/index.html` with identical component structure and localized content.

- [ ] **Step 1: Write a failing rendered-home test**

```js
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../', import.meta.url);
const html = (path) => readFile(new URL(`dist/${path}`, root), 'utf8');

test('renders the localized home routes with reciprocal language links', async () => {
  const [zh, en] = await Promise.all([html('index.html'), html('en/index.html')]);
  assert.match(zh, /小而完整，真正能行动/);
  assert.match(en, /Small by design\. Ready to act\./);
  assert.match(zh, /hreflang="en"/);
  assert.match(en, /hreflang="zh-CN"/);
});

test('renders the real Claw Trace states and install command', async () => {
  const zh = await html('index.html');
  for (const step of ['MESSAGE_RECEIVED', 'POLICY_CHECK', 'APPROVAL', 'TOOL_EXECUTION', 'RESULT_DELIVERED']) {
    assert.match(zh, new RegExp(step));
  }
  assert.match(zh, /git clone https:\/\/github\.com\/NEDONION\/miniclaw\.git/);
  assert.match(zh, /aria-live="polite"/);
});
```

- [ ] **Step 2: Run the rendered-home test before creating pages**

Run: `node --test website/tests/rendered-home.test.mjs`

Expected: FAIL with `ENOENT` for `dist/index.html`.

- [ ] **Step 3: Implement the semantic base layout and header**

The base layout sets localized `<html lang>`, title, description, canonical URL, reciprocal `hreflang`,
Open Graph metadata, favicon, skip link, header, main landmark, and footer. The header contains Product,
Safety, Channels, Docs, GitHub, and the reciprocal language route.

- [ ] **Step 4: Implement the Hero, command copy interaction, and Claw Trace**

The Hero contains the approved eyebrow, localized thesis, two CTAs, and an accessible command block.
The copy script uses `navigator.clipboard.writeText`, changes the status text for two seconds, and restores
the original label. Claw Trace renders ordered event rows and uses CSS classes for waiting/running/succeeded.

- [ ] **Step 5: Add the responsive global token system**

Define the eight approved colors, Archivo/IBM Plex Mono/Newsreader roles, 12-column desktop grid,
mobile vertical trace, visible focus rings, and reduced-motion overrides. Do not add gradients, glass cards,
scroll hijacking, WebGL, or a custom cursor.

- [ ] **Step 6: Build and run the rendered-home test**

Run: `npm run build`

Run: `node --test tests/rendered-home.test.mjs`

Expected: 2 tests PASS.

- [ ] **Step 7: Commit the bilingual homepage foundation**

```bash
git add website
git commit -m "feat(website): 构建双语 Hero 与 Claw Trace"
```

### Task 3: Add the evidence narrative, real workflows, and visual assets

**Files:**
- Modify: `website/tests/rendered-home.test.mjs`
- Create: `website/src/components/EvidenceRail.astro`
- Create: `website/src/components/RuntimeFlow.astro`
- Create: `website/src/components/SurfaceSection.astro`
- Create: `website/src/components/SafetySection.astro`
- Create: `website/src/components/MemorySection.astro`
- Create: `website/src/components/WorkflowGallery.astro`
- Create: `website/src/components/QuickStart.astro`
- Modify: `website/src/components/HomePage.astro`
- Modify: `website/src/styles/global.css`
- Create: `website/public/images/miniclaw-tui-approval-warp.png`
- Create: `website/public/images/miniclaw-tui-external-cli-warp.png`

**Interfaces:**
- Consumes: shared facts and localized section content.
- Produces: complete marketing narrative with two tracked, real screenshots.

- [ ] **Step 1: Extend the rendered-home test with evidence and disclosure assertions**

```js
test('shows evidence without overstating live acceptance', async () => {
  const [zh, en] = await Promise.all([html('index.html'), html('en/index.html')]);
  assert.match(zh, /33 条 versioned Channel cases/);
  assert.match(zh, /Implementation PASS 不等于 Live PASS/);
  assert.match(en, /Implementation PASS is not Live PASS/);
  assert.match(zh, /miniclaw-tui-approval-warp\.png/);
  assert.match(zh, /miniclaw-tui-external-cli-warp\.png/);
});
```

- [ ] **Step 2: Run the test and verify the missing evidence sections fail it**

Run: `node --test website/tests/rendered-home.test.mjs`

Expected: FAIL because the evidence sentence is absent.

- [ ] **Step 3: Implement the six remaining narrative components**

Use ordered semantics only for the real runtime flow. Use comparison rows and bordered editorial panels for
surfaces, safety, and memory. Quick Start renders the same command string as the Hero and links to `/docs/getting-started/`.

- [ ] **Step 4: Reuse the two current product screenshots**

Copy `docs/assets/miniclaw-tui-approval-warp.png` and `docs/assets/miniclaw-tui-external-cli-warp.png`
byte-for-byte into `website/public/images/`. Do not use the older conversation screenshot because its Memory copy is stale.

- [ ] **Step 5: Build and run all website tests**

Run: `npm run build && npm test`

Expected: all content and rendered-home tests PASS.

- [ ] **Step 6: Commit the complete marketing narrative**

```bash
git add website
git commit -m "feat(website): 加入工程证据与真实工作流"
```

### Task 4: Add curated bilingual Starlight documentation

**Files:**
- Create: `website/tests/rendered-docs.test.mjs`
- Create: `website/src/styles/docs.css`
- Create: `website/src/content/docs/docs/index.mdx`
- Create: `website/src/content/docs/docs/getting-started.mdx`
- Create: `website/src/content/docs/docs/runtime.mdx`
- Create: `website/src/content/docs/docs/security.mdx`
- Create: `website/src/content/docs/docs/channels.mdx`
- Create: `website/src/content/docs/docs/memory.mdx`
- Create: `website/src/content/docs/en/docs/index.mdx`
- Create: `website/src/content/docs/en/docs/getting-started.mdx`
- Create: `website/src/content/docs/en/docs/runtime.mdx`
- Create: `website/src/content/docs/en/docs/security.mdx`
- Create: `website/src/content/docs/en/docs/channels.mdx`
- Create: `website/src/content/docs/en/docs/memory.mdx`

**Interfaces:**
- Consumes: Starlight root/en locale configuration and the shared public GitHub links.
- Produces: matching `/docs/...` and `/en/docs/...` route sets.

- [ ] **Step 1: Write the failing docs route test**

```js
import assert from 'node:assert/strict';
import { access, readFile } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../dist/', import.meta.url);
const routes = ['docs', 'docs/getting-started', 'docs/runtime', 'docs/security', 'docs/channels', 'docs/memory'];

test('builds matching Chinese and English documentation routes', async () => {
  for (const route of routes) {
    await access(new URL(`${route}/index.html`, root));
    await access(new URL(`en/${route}/index.html`, root));
  }
});

test('keeps live-status language explicit in both documentation sets', async () => {
  const zh = await readFile(new URL('docs/index.html', root), 'utf8');
  const en = await readFile(new URL('en/docs/index.html', root), 'utf8');
  assert.match(zh, /真实平台 Live Gate/);
  assert.match(en, /real-platform Live Gate/);
});
```

- [ ] **Step 2: Run the test and verify the missing docs routes fail it**

Run: `node --test website/tests/rendered-docs.test.mjs`

Expected: FAIL with `ENOENT` for `dist/docs/index.html`.

- [ ] **Step 3: Write the six Chinese user-facing documents**

Use current README commands and boundaries. Each document links to the deeper GitHub source document rather than
copying internal planning material. Security and Channels pages explicitly distinguish local implementation evidence
from live platform evidence.

- [ ] **Step 4: Write the complete English counterparts**

Use matching filenames, headings, command blocks, warnings, and GitHub deep links so Starlight can associate translations.

- [ ] **Step 5: Theme Starlight with the marketing tokens**

Map Starlight accent, background, border, font, code, focus, and content-width variables to the website system.
Keep Starlight's built-in accessible navigation, search-ready structure, and light/dark preference.

- [ ] **Step 6: Build and run all website tests**

Run: `npm run build && npm test`

Expected: all tests PASS and all 12 docs pages exist.

- [ ] **Step 7: Commit curated documentation**

```bash
git add website
git commit -m "docs(website): 增加中英文用户文档"
```

### Task 5: Verify type safety, build output, browser behavior, and repository hygiene

**Files:**
- Modify: `website/package.json`
- Modify: `website/tests/rendered-home.test.mjs`
- Create: `website/README.md`

**Interfaces:**
- Consumes: complete website and repository documentation entry points.
- Produces: reproducible local quality gate and discoverable website source location.

- [ ] **Step 1: Add a failing assertion for website source documentation**

Add a test that reads `README.md` inside the website package and requires it to state the Vercel Root Directory,
build command, and output directory. Run it before creating the file and verify it fails.

- [ ] **Step 2: Document local website development and deployment**

Add concise local commands to `website/README.md`:

```bash
cd website
npm install
npm run dev
npm run check
npm run build
npm test
```

State that the website source is isolated in `website/`, Vercel Root Directory is `website`, and no server adapter
is required. Do not modify the repository root READMEs because the user's main worktree already contains unrelated,
uncommitted README edits.

- [ ] **Step 3: Run the complete website quality gate**

Run: `npm run check`

Run: `npm run build`

Run: `npm test`

Run: `git diff --check`

Expected: all commands exit 0.

- [ ] **Step 4: Start the dev server and run browser verification**

Run: `npm run dev -- --host 127.0.0.1`

Verify `/`, `/en/`, `/docs/`, and `/en/docs/` load; capture desktop and mobile screenshots; confirm no blank page,
no framework error overlay, no console errors, visible language/docs links, working copy button, and reduced-motion behavior.

- [ ] **Step 5: Commit verified website documentation**

```bash
git add website
git commit -m "docs(website): 补充本地开发与 Vercel 部署"
```

### Task 6: Create and verify the Vercel Preview deployment

**Files:**
- No source file changes expected.

**Interfaces:**
- Consumes: verified static `website/` project.
- Produces: one Vercel Preview URL and deployment metadata.

- [ ] **Step 1: Confirm the Vercel team and existing project state**

Use the connected Vercel tools to list teams and projects. Reuse a project only if it is already named for MiniClaw;
otherwise deploy the current website as a new project with Root Directory `website`.

- [ ] **Step 2: Deploy a Preview from the verified website directory**

Use the connected `deploy_to_vercel` operation from the worktree. Do not promote to Production before the Preview
returns `READY` and browser verification succeeds.

- [ ] **Step 3: Inspect the deployment and build logs**

Confirm status `READY`, detected framework `Astro`, and a successful static build. If the build fails, fetch error-only
build logs, fix the first source issue, rerun local checks, and redeploy once.

- [ ] **Step 4: Verify the deployed user story**

Open the Preview `/`, `/en/`, `/docs/`, and `/en/docs/` routes. Confirm the same headings, language links, screenshots,
command copy behavior, and absence of runtime errors as the local build.

- [ ] **Step 5: Report the deployment result**

Report URL, target `preview`, status, commit, framework, and build duration. Keep Production promotion as a separate,
explicit follow-up after the user has seen the Preview.
