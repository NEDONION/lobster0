# MiniClaw Production-grade Website Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有三屏官网实现为已确认的浅色高密度产品站，保留原始三箭头 Logo，并通过真实内容、中文本地化、协调动画和浏览器截图达到成熟开源 Agent 官网水准。

**Architecture:** 保留单一 Next.js 16 + Fumadocs 应用和现有三段首页结构。复用内容契约、Tabs、Motion 与真实 TUI 图片，只新增一个可复用箭头 Logo 组件，并在现有营销组件和全局样式内完成视觉重构。

**Tech Stack:** Next.js 16.3 App Router、React 19、TypeScript 6、Motion 13、Fumadocs 16、Vitest、Testing Library、Playwright、Vercel。

## Global Constraints

- 首页必须保持三个 `main > section.marketing-section`，文档路由和 Fumadocs 不变。
- 中文页面除产品名、代码、命令、事件标识和专有名词外使用中文。
- Logo 固定为最初的蓝、绿、浅色三箭头，不再探索机械爪或字母 `M`。
- Hero 标题桌面端最大 `54px`，Section 标题桌面端最大 `36px`。
- 不新增 UI、动画或字体依赖；复用 Motion、Instrument Sans、系统中文字体和 IBM Plex Mono。
- 动画必须尊重 `prefers-reduced-motion`，关键内容无动画仍完整可读。
- 不添加尚未实现的产品能力、虚构后台、客户评价或实时外部数据。

---

### Task 1: Restore the arrow brand and rebuild the centered hero

**Files:**
- Create: `website/src/components/marketing/BrandMark.tsx`
- Modify: `website/src/components/marketing/MarketingHeader.tsx`
- Modify: `website/src/components/marketing/HeroRuntime.tsx`
- Modify: `website/src/components/marketing/ClawTrace.tsx`
- Modify: `website/src/components/marketing/EvidenceStrip.tsx`
- Modify: `website/src/content/site.ts`
- Test: `website/src/components/marketing/MarketingHome.test.tsx`
- Test: `website/src/components/marketing/ClawTrace.test.tsx`

**Interfaces:**
- Consumes: `siteFacts.surfaces`, `siteFacts.traceEvents`, `MarketingCopy['hero']`, `MarketingCopy['trace']`。
- Produces: `BrandMark({ className?, title?, size? })`；Hero 中的 `.hero-network`、`.hero-surface-card`、`[data-brand-mark]` 和动态 Trace 状态。

- [ ] **Step 1: Write failing tests for the restored brand and localized hero network**

```tsx
it('uses the three-arrow brand and localized surface cards', () => {
  const { container } = render(<MarketingHome locale="zh-CN" />);

  expect(container.querySelectorAll('[data-brand-mark]')).toHaveLength(2);
  expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('你的本地行动助手');
  expect(screen.getByText('本地界面')).toBeInTheDocument();
  expect(screen.getByText('工作入口')).toBeInTheDocument();
  expect(screen.getByText('移动入口')).toBeInTheDocument();
  expect(screen.getByText('社区入口')).toBeInTheDocument();
});
```

```tsx
it('keeps all six real runtime events in the animated hero signal', () => {
  setReducedMotion(false);
  render(<ClawTrace copy={marketingCopy['zh-CN'].trace} surfaces={marketingCopy['zh-CN'].hero.surfaces} />);

  expect(screen.getByRole('list', { name: 'MiniClaw 运行轨迹' })).toHaveTextContent('POLICY_CHECK');
  expect(screen.getAllByRole('listitem')).toHaveLength(10);
});
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `npm test -- MarketingHome.test.tsx ClawTrace.test.tsx`

Expected: FAIL because `BrandMark`, `hero.surfaces`, the centered Hero title, and new Trace markup do not exist.

- [ ] **Step 3: Implement the reusable SVG mark and centered Hero**

```tsx
export function BrandMark({ className, size = 40, title }: BrandMarkProps) {
  return (
    <svg data-brand-mark className={className} width={size} height={size} viewBox="0 0 64 64" role={title ? 'img' : undefined} aria-hidden={title ? undefined : true}>
      {title ? <title>{title}</title> : null}
      <rect width="64" height="64" rx="14" fill="currentColor" />
      <path className="brand-mark__arrow brand-mark__arrow--blue" d="M14 17 29 32 14 47" />
      <path className="brand-mark__arrow brand-mark__arrow--green" d="m28 12 16 20-16 20" />
      <path className="brand-mark__arrow brand-mark__arrow--light" d="m43 18 9 14-9 14" />
    </svg>
  );
}
```

`HeroRuntime` 改为居中内容；`ClawTrace` 改为环绕 Hero 的四入口卡、轨迹 SVG 和六步状态条。保留原有 `requestAnimationFrame` 驱动和 reduced-motion 最终态，不添加计时器。

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `npm test -- MarketingHome.test.tsx ClawTrace.test.tsx`

Expected: PASS.

- [ ] **Step 5: Commit the brand and Hero task**

```bash
git add website/src/components/marketing/BrandMark.tsx website/src/components/marketing/MarketingHeader.tsx website/src/components/marketing/HeroRuntime.tsx website/src/components/marketing/ClawTrace.tsx website/src/components/marketing/EvidenceStrip.tsx website/src/content/site.ts website/src/components/marketing/MarketingHome.test.tsx website/src/components/marketing/ClawTrace.test.tsx
git commit -m "feat(website): 恢复 arrow brand 并重构产品化 Hero"
```

### Task 2: Localize the product panels and workbench chrome

**Files:**
- Modify: `website/src/content/site.ts`
- Modify: `website/src/components/marketing/CapabilityExplorer.tsx`
- Modify: `website/src/components/marketing/capabilities/RuntimePanel.tsx`
- Modify: `website/src/components/marketing/capabilities/ChannelsPanel.tsx`
- Modify: `website/src/components/marketing/capabilities/SafetyPanel.tsx`
- Modify: `website/src/components/marketing/capabilities/MemoryPanel.tsx`
- Modify: `website/src/components/marketing/capabilities/AutomationPanel.tsx`
- Modify: `website/src/components/marketing/Workbench.tsx`
- Modify: `website/src/components/marketing/MultiChannelDiagram.tsx`
- Modify: `website/src/components/marketing/QuickStartClose.tsx`
- Test: `website/src/components/marketing/MarketingHome.test.tsx`
- Test: `website/src/components/marketing/Workbench.test.tsx`

**Interfaces:**
- Consumes: `Locale` and localized `MarketingCopy`.
- Produces: `CapabilityCopy.eyebrow` and locale-aware map/readout labels; existing public component names remain unchanged.

- [ ] **Step 1: Write failing Chinese-localization assertions**

```tsx
it('keeps Chinese structural labels around technical identifiers', () => {
  render(<MarketingHome locale="zh-CN" />);

  expect(screen.getByText('01 / 核心循环')).toBeInTheDocument();
  expect(screen.getByText('默认状态')).toBeInTheDocument();
  expect(screen.getByText('仓库真实图片')).toBeInTheDocument();
  expect(screen.getByText('可观察步骤')).toBeInTheDocument();
});
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `npm test -- MarketingHome.test.tsx Workbench.test.tsx`

Expected: FAIL on the currently hard-coded English structural labels.

- [ ] **Step 3: Add localized structural copy and pass locale to panels**

Add `eyebrow` to each capability copy, pass `locale` from `CapabilityExplorer`, and use a small local `labels` object in each component where only map chrome changes. Keep `AgentRuntime`, `Policy`, `Tool`, `Markdown`, `SQLite`, `SAFE`, `SMART`, `AUTOPILOT`, and `YOLO` as technical identifiers with Chinese context.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `npm test -- MarketingHome.test.tsx Workbench.test.tsx`

Expected: PASS.

- [ ] **Step 5: Commit localized interface chrome**

```bash
git add website/src/content/site.ts website/src/components/marketing/CapabilityExplorer.tsx website/src/components/marketing/capabilities website/src/components/marketing/Workbench.tsx website/src/components/marketing/MultiChannelDiagram.tsx website/src/components/marketing/QuickStartClose.tsx website/src/components/marketing/MarketingHome.test.tsx website/src/components/marketing/Workbench.test.tsx
git commit -m "refactor(website): 完成中文-first product chrome"
```

### Task 3: Apply the production visual system and motion choreography

**Files:**
- Modify: `website/src/styles/globals.css`
- Modify: `website/tests/e2e/marketing.spec.ts`

**Interfaces:**
- Consumes: Existing marketing class names plus Task 1 `.hero-network`, `.hero-surface-card`, `.brand-mark__arrow`.
- Produces: Six-token palette, compact type scale, responsive three-screen layout, semantic card accents, coordinated CSS keyframes and reduced-motion behavior.

- [ ] **Step 1: Write failing browser assertions for visual constraints**

```ts
const heroSize = await page.locator('#hero-title').evaluate((node) => parseFloat(getComputedStyle(node).fontSize));
const sectionSize = await page.locator('#product-title').evaluate((node) => parseFloat(getComputedStyle(node).fontSize));
expect(heroSize).toBeLessThanOrEqual(54);
expect(sectionSize).toBeLessThanOrEqual(36);
await expect(page.locator('[data-brand-mark]').first()).toBeVisible();
await expect(page.locator('.hero-surface-card')).toHaveCount(4);
```

For reduced motion:

```ts
const animationDuration = await page.locator('.hero-network__signal').first().evaluate(
  (node) => getComputedStyle(node).animationDuration,
);
expect(['0s', '0.001s', '0.00001s']).toContain(animationDuration);
```

- [ ] **Step 2: Run Playwright and verify RED**

Run: `npm run test:e2e -- marketing.spec.ts`

Expected: FAIL because current titles exceed the limits and the new Hero visual classes are absent.

- [ ] **Step 3: Rewrite the existing marketing CSS in place**

Use the approved palette: Canvas `#F4F7FB`, Paper `#FFFFFF`, Ink `#121722`, Signal Blue `#4267F5`, Coral `#F16C56`, Green `#2CAF88`; Amber and Violet remain small semantic accents. Set Hero `clamp(2.5rem, 4vw, 3.375rem)` and Section headings `clamp(2rem, 2.8vw, 2.25rem)`. Add one coordinated load sequence, arrow draw, route dash, signal travel, offset surface float, Tab transition, image sheen, and restrained hover motion. Keep the existing global reduced-motion override.

- [ ] **Step 4: Run Playwright and verify GREEN**

Run: `npm run test:e2e -- marketing.spec.ts`

Expected: PASS on desktop and configured mobile project, with no horizontal overflow or page errors.

- [ ] **Step 5: Commit the visual system**

```bash
git add website/src/styles/globals.css website/tests/e2e/marketing.spec.ts
git commit -m "style(website): 提升 Claw-like visual precision 与 motion"
```

### Task 4: Align social branding and complete visual verification

**Files:**
- Modify: `website/src/app/opengraph-image.tsx`
- Modify: `website/public/favicon.svg`
- Modify: `website/tests/e2e/metadata.spec.ts`

**Interfaces:**
- Consumes: The approved arrow geometry and website palette.
- Produces: Matching favicon and 1200×630 Open Graph image without the `M` placeholder.

- [ ] **Step 1: Add a failing metadata test for the generated image**

```ts
await page.goto('/opengraph-image');
expect((await page.locator('body').screenshot()).byteLength).toBeGreaterThan(10_000);
```

Also assert the homepage icon link resolves successfully and the OG route returns `image/png` through the existing metadata browser test.

- [ ] **Step 2: Run the metadata test and verify RED**

Run: `npm run test:e2e -- metadata.spec.ts`

Expected: FAIL on the new arrow-brand expectation while the current OG image still contains the `M` placeholder.

- [ ] **Step 3: Replace the OG placeholder with the three-arrow geometry**

Use Satori-compatible flexbox and inline SVG/path elements only. Reuse the final palette and compact Hero message; do not add file reads, fonts or external requests.

- [ ] **Step 4: Run all website and repository verification**

Run:

```bash
npm run typecheck
npm run lint
npm test
npm run build
npm run test:e2e
uv run python scripts/validate_docs.py
git diff --check
```

Expected: all commands PASS.

- [ ] **Step 5: Perform screenshot critique and deploy a fresh Preview**

Start the production server, capture desktop `1440×1000` and mobile `390×844` screenshots, and check: arrow Logo consistency, title scale, Chinese coverage, card alignment, animation hierarchy, three-section density, real TUI legibility, focus states, and no console errors. Fix any visible defect before running the verification commands again.

Deploy the verified commit to the existing Vercel `miniclaw` project as Preview, inspect `/`, `/en`, `/docs`, `/en/docs`, `/opengraph-image`, `/sitemap.xml`, and `/robots.txt`, then promote only this artifact to Production.

- [ ] **Step 6: Commit the social brand and final fixes**

```bash
git add website/src/app/opengraph-image.tsx website/public/favicon.svg website/tests/e2e/metadata.spec.ts
git commit -m "feat(website): 统一 arrow brand social assets"
```
