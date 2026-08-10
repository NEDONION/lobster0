# MiniClaw 官网设计改动记录 · Round 2

> 本文档按用户要求（"改的时候和改之前设计的时候一定要同步给我落库文档，记录详细"）落库，记录本轮改动的触发反馈、设计决策与改动明细。后续每轮设计改动都应在本文件追加一个新的 `## Round N` 小节，而不是另开文件，除非改动跨度足够大值得单独立项。

## Round 2（2026-08-10）

### 触发反馈（用户原话）

1. "上面【能力、实证】给我换个词行吗 这都是什么意思？给我模仿开源项目官网的用词。"
2. "下面的动画和样式，大片大片的白色卡片+文字 就这？？一点设计感都没有 还占据了大量的位置"
3. "还有官网上很不专业的字【33 条 versioned Channel cases；15 条 versioned Automation cases。Implementation PASS 不等于 Live PASS。】都给我去掉啊"
4. "这个本地启动的代码块要平铺啊 为什么还要让 visitor 去上下滑动"

### 决策与改动

#### 1. 导航栏文案：能力/实证 → 特性/演示

- **问题**：`能力`/`实证` 是内部术语，访客看不懂在讲什么，也不像开源项目官网的用词习惯（对标 Vercel/Supabase 等常用 Features / Demo）。
- **改动**：`nav.product` 中文从 `能力` 改为 `特性`，英文 `Capabilities` 改为 `Features`；`nav.workbench` 中文从 `实证` 改为 `演示`，英文 `Evidence` 改为 `Demo`。对应 section 的 eyebrow（`能力视图 / 02` → `特性 / 02`，`真实实证 / 03` → `演示 / 03`，英文同步）。
- **文件**：`website/src/content/site.ts`（`nav`、`product.eyebrow`、`workbench.eyebrow`，中英两个 locale）。

#### 2. 运行时 Trace 面板重做：卡片网格 → 连接式时间线

- **问题**：`能力 / 01 运行时` tab 下的六个状态（`MESSAGE_RECEIVED` … `RESULT_DELIVERED`）此前是 3×2 的空心卡片网格，每张卡片 `min-height: 118px` 但内容只有一个序号 + 一行文字，视觉上是"大片白色卡片"，且不传达真正有价值的信息（trace 的 `state` / `detail` 字段之前压根没有渲染出来，虽然 `content/site.ts` 里已经有 `steps[].state` 和 `steps[].detail`）。
- **改动**：把 `RuntimePanel` 从渲染 `siteFacts.traceEvents`（只有事件名）改成渲染 `marketingCopy[locale].trace.steps`（事件名 + state + detail 都有），UI 从卡片网格改成一条带连接线（rail）+ 状态圆点 + 状态徽标（pill）的竖向时间线，用颜色区分 `allowed/succeeded`（绿）、`waiting`（黄）、`running`（蓝）。
- **文件**：
  - `website/src/components/marketing/capabilities/RuntimePanel.tsx`（改用 `<ol>` + `data-state`，渲染 state/detail）
  - `website/src/styles/globals.css`（新增 `.runtime-map` 系列时间线样式，删除旧的卡片网格样式）
  - `website/src/components/marketing/MarketingHome.module.css`（删除针对旧卡片 `nth-child` 描边色的规则）

#### 3. 移除各 section 的强制视口高度（页面"占据大量空间"的真正原因）

- **根因排查**：实测发现真正撑大空白的不是卡片本身，而是 `.marketing-section { min-height: 88svh }`（以及 `.capability-explorer` 86svh、`.workbench` 92svh、`.capability-panel` 390px 最小高度）——不管内容多短，每个 section 都被强制撑到接近一屏。
- **改动**：把这几处 `min-height: 8x-9xsvh` 去掉（改为 `min-height: 0` 或直接删除），`.capability-panel` 的固定 `390px` 也删除，改为内容自适应高度；顺带把 section 的上下 padding 从 56-58px 收紧到 46-48px。
- **验证**：E2E 里 `marketing.spec.ts` 对总视口高度有一个上限断言（`scrollHeight / innerHeight <= 3.2`），只卡上限不卡下限，所以收紧高度不会破坏该测试的语义。
- **文件**：`website/src/components/marketing/MarketingHome.module.css`、`website/src/styles/globals.css`

#### 4. 移除首页"33 条 versioned Channel cases…Implementation PASS 不等于 Live PASS"提示条

- **问题**：这行字是内部工程话术（区分"实现完成"和"线上验证通过"），面向访客的营销页出现这种表述不专业。
- **改动**：从 `EvidenceStrip` 组件里删掉渲染这行文案的 `<p>`。**没有删除底层数据字段 `evidence.disclosure`**（`content/site.ts` 里还在，`site.test.ts` 里还有一条断言检查这个字段包含 "Live PASS"）——因为这个"未验证 ≠ 已上线"的诚实声明本身是这个项目一贯坚持的原则（在 `05 自动化` 能力面板的 fact 列表里还有一条 `Implementation ≠ Live` 徽标承载同样的意思），只是不需要在首页统计条上再重复一遍生硬的中文长句。
- **文件**：`website/src/components/marketing/EvidenceStrip.tsx`

#### 5. "本地启动"代码块不再要求访客上下滚动

- **问题**：`.quick-start-close > .command-copy pre { max-height: 110px }` 把安装命令（9 行）压缩到只显示 4 行左右，访客要在一个本来就不大的卡片里再滚动一次才能看完整安装步骤。
- **改动**：把这条规则的 `max-height: 110px` 改成 `max-height: none`，让代码块按内容自然铺开。
- **文件**：`website/src/styles/globals.css`

### 已知未完成 / 建议下一轮处理

用户在反馈里用红框圈出的 `SAFE 审批` workflow 面板的"来源 / 分辨率 / 状态"三行元信息，目前仍是纯文本 `<dl>` 列表（`Workbench.tsx` + 对应 CSS），没有在本轮一起重做。以及能力面板里除"运行时"之外的另外四个（`多入口` / `安全边界` / `记忆` / `自动化`）用的还是旧的 `channels-map` / `safety-map` / `memory-map` / `automation-map` 卡片式布局，视觉语言和刚重做的 `runtime-map` 时间线不统一。这两块建议作为 Round 3 处理，本轮为了尽快按用户"赶紧改完 push"的要求先收敛已确认的四项。

### 验证

- `npx tsc --noEmit`：通过
- `npx eslint src/`：通过
- `npx vitest run`：28/28 通过
- E2E（`npx playwright test`）：在改动前后各跑过一次全量对比，8 个失败项（`marketing.spec.ts` 的 tab/reduced-motion 断言、`docs.spec.ts` 的搜索弹窗超时）在**没有本轮任何改动的 `b1819fc`** 上同样失败，判定为本地 `next dev` 环境已有的时序 flaky，与本轮改动无关，未继续深挖。

## Round 3（2026-08-10，进行中）

### 触发反馈

用户对 Round 2 结尾提出的待办确认："好的 好的 继续"——即继续处理 SAFE 审批面板的"来源/分辨率/状态"元信息条，以及统一另外四个能力面板（多入口/安全边界/记忆/自动化）的视觉语言。

### 已完成

#### workflow 面板元信息条：堆叠 key-value 行 → 横向 stat 卡片

- **问题**：`SAFE 审批` / `外部 CLI` workflow 面板底部的"来源 / 分辨率 / 状态"是三行纯文本 `<dl>`，每行用 `justify-content: space-between` + 顶部分隔线，视觉上就是用户截图里红框圈出的那种"纯文字堆砌"。
- **改动**：复用页面已经验证过的 `EvidenceStrip`（首页统计条）视觉语言——label 在上（小号灰色 mono）、value 在下（大号加粗 mono），三列横排，用竖线分隔而不是横线堆叠。
- **文件**：`website/src/styles/globals.css`（`.workflow-panel__copy > dl` 及其子选择器）。
- **验证现状**：`tsc` / `eslint` / `vitest`（28/28）全部通过。**本次浏览器可视化验证被环境问题阻塞**——Browser 面板在本轮会话中途开始持续返回 `innerWidth/innerHeight: 0`（含新开的干净 tab），截图全部空白，无法用 DOM 测量或截图做实际视觉确认。这个 CSS 改动是对已经视觉确认过的 `.evidence-strip` 结构做的同构复用（只改了列数和分隔方式），风险评估为低，但**下次会话/面板恢复后应优先补一次真实截图确认**。

### 待办（未开始）

`多入口` / `安全边界` / `记忆` / `自动化` 四个能力面板的视觉统一，因浏览器面板故障暂停，等可视化验证恢复后再继续，避免在看不到渲染结果的情况下对结构差异较大的四个组件做批量视觉改动。

## Round 4（2026-08-10）

### 触发反馈（用户原话，来自线上 miniclaw.jchu.tech 的真实截图）

1. 首页统计条（01/04/18/33/15）右侧有一大块空白方块。
2. 语言切换用纯文字"English"链接，要求换成标准化图标（globe），并且要为将来加日语/法语这类多语言留出扩展空间。
3. "特性"里 02-05（多入口/安全边界/记忆/自动化）"太敷衍了简直没法看"，要求对标开源项目生产级别的精致样式、UI 与动画。
4. "演示"区域不要用截图，要求手绘动画展示数据流向；理由是产品还在快速开发中，截图会很快过时，没法每次开发完都手动更新。

### 已完成

#### 1. 统计条空白 —— 真正的第二次修复

Round 3 只改了 `globals.css` 里的 `.evidence-strip`，但 `MarketingHome.module.css`（CSS Module，作用域选择器 `.root :global(...)` 优先级更高）里有一条同名规则仍然维持 `grid-template-columns: minmax(0, 8fr) minmax(300px, 4fr)` 两列布局——这才是留白的真正原因。用 Playwright + 系统 Chrome（`channel: 'chrome'`）截图实测确认修复生效（见下方"可视化验证方式恢复"）。同时清理了随 Round 2 移除 disclosure 文案后失效的 `.evidence-strip p` 系列死样式（`globals.css` 与 module css 两处）。

**文件**：`website/src/components/marketing/MarketingHome.module.css`、`website/src/styles/globals.css`

#### 2. 语言切换器：文字链接 → 图标下拉菜单

- **改动**：新增 `LanguageSwitcher.tsx`（client component），toggle 按钮用地球图标 + 当前语言缩写（"中"/"EN"），点击展开下拉菜单列出所有可用 locale。
- **可扩展性**：菜单项从 `lib/i18n.ts` 新增的 `locales` 数组和 `localeNames` 映射渲染，不是写死两个选项——以后要加日语/法语，只需要在 `localeNames` 里加一条映射、在 `locales` 数组加一个值，UI 不用改。
- **连带修复**：`nav.language` 字段原本存的是"对方语言的名字"（zh-CN 页面存 'English'），语义上是给旧的纯文字链接用的；现在改成语言切换按钮的 aria-label（"切换语言" / "Switch language"）。
- **文件**：`website/src/lib/i18n.ts`（新增 `localeNames`）、`website/src/components/marketing/LanguageSwitcher.tsx`（新建）、`website/src/components/marketing/MarketingHeader.tsx`、`website/src/content/site.ts`、`website/src/styles/globals.css`（新增 `.language-switch` 系列，删除死掉的 `.language-link`）。
- **测试**：更新了 `MarketingHome.test.tsx`（用 `userEvent` 点开下拉再断言选项）和 `tests/e2e/marketing.spec.ts`（同样先点 toggle 再点 `English` option）。

#### 3. 可视化验证方式恢复（不再依赖 Browser 面板）

Round 3 记录的 Browser 面板故障（`innerWidth/innerHeight` 持续为 0）在本轮会话里依然没有恢复，重启、resize、新开 tab 都没用。改用 **Playwright + 系统 Chrome（`channel: 'chrome'`，绕开需要单独下载的 Playwright 自带浏览器）**在 `website/` 目录下跑一个一次性脚本，把 `page.screenshot()` 存到 `/tmp/mc-shots/`，再用 Read 工具直接读 PNG 文件——这条路径全程独立于坏掉的 Browser 面板，本轮开始的所有可视化改动都用这个方式实测验证。脚本本身不提交（加进了 `website/.gitignore` 的 `.scratch-*` 忽略规则）。

#### 4. 演示区域：真实 TUI 截图 → 手绘动画数据流

- **决策依据**：用户明确要求"手绘动画+动画数据流都可以，不要用截图"，理由是产品还在快速迭代，截图会持续落后于实际进度、没人会每次开发完都手动重截图更新。这与页面原有的 "看真实执行，不看概念渲染" slogan 存在字面上的张力，处理方式是把动画内容锚定在**真实的执行机制**上（argv 结构、审批门禁、隔离执行、结果返回——这些和页面其它地方的 Trace 步骤、Automation fact 是同一套事实），不是编造的功能预览图，slogan 相应改写为"看真实机制，不看功能截图"，保持诚实这条原则不变，只是不再要求它必须是一张像素级截图。
- **改动**：
  - `content/site.ts`：`WorkflowCopy.image` 字段整个替换成 `WorkflowCopy.flow`（`FlowStepCopy[]`，含 icon/label/detail/state），给 `approval` 写了 5 步（用户请求 → exact argv → Owner 审批 → 隔离执行 → 结果返回），给 `external-cli` 写了 4 步（程序 → argv[] → 隔离子进程 → 结构化结果），中英文各一份。
  - 新增 `FlowIcon.tsx`（6 个手绘 monoline SVG 图标：intent/argv/gate/run/result/program）和 `FlowDiagram.tsx`（client component：IntersectionObserver 触发的交错入场动画 + 连接线上的常驻流动光效，尊重 `useReducedMotionPreference`，复用 `ClawTrace.tsx` 已经验证过的“进入视口再播放”模式）。
  - `Workbench.tsx` 里原来渲染 `<Image>` 的 `ScreenshotWorkflow` 改名 `FlowWorkflow`，改渲染 `<FlowDiagram>`；meta 条的"来源/分辨率/状态"改成"步骤/来源/状态"（"来源"从"仓库真实图片"改成"真实执行链路"，仍然诚实——链路内容是真的，只是载体不是截图）。
  - 删除了 `public/images/miniclaw-tui-approval-warp.{png,webp}` 和 `miniclaw-tui-external-cli-warp.{png,webp}`——确认过仓库根目录 `README.md`/`README_EN.md` 用的是 `docs/assets/` 下的独立副本，不受影响。
  - `workbench.eyebrow`/`title`/`lead` 文案同步更新（中英文），不再提"两张 TUI 截图"。
  - `globals.css` 里 `.workflow-shot` 系列（含 `figcaption`、`img` 悬浮缩放）整体替换为 `.flow-diagram` 系列（暗色终端底 + 节点连接线 + 流动光效 + 等待态呼吸动画）；`MarketingHome.module.css` 里的页面级 sheen 扫光效果同步从 `.workflow-shot` 重命名到 `.flow-diagram`，继续复用。
  - 更新了 `Workbench.test.tsx`（不再断言 `<img>`，改断言 `FlowDiagram` 的 `<ol aria-label>` 和步骤文案）。
- **可视化验证**：桌面（1400px）两个 tab、移动端（390px）都用上述 Playwright 截图方式实测确认——节点入场动画、状态配色（等待=琥珀呼吸光、执行中=蓝、已完成=绿）、连接线流动光效、移动端换行布局全部符合预期。
- **文件**：`website/src/content/site.ts`、`website/src/components/marketing/FlowIcon.tsx`（新建）、`website/src/components/marketing/FlowDiagram.tsx`（新建）、`website/src/components/marketing/Workbench.tsx`、`website/src/components/marketing/Workbench.test.tsx`、`website/src/styles/globals.css`、`website/src/components/marketing/MarketingHome.module.css`、`website/public/images/`（删除 4 个文件）。

### 验证

- `npx tsc --noEmit`：通过
- `npx eslint src/`：通过
- `npx vitest run`：28/28 通过
- 可视化：Playwright + 系统 Chrome 截图确认（详见上文），不再依赖坏掉的 Browser 面板

### 待办

`安全边界` / `记忆` / `自动化` 三个能力面板的视觉统一还没做。

## Round 5（2026-08-10，进行中）

### `多入口`（ChannelsPanel）重做：卡片网格 → 暗色 hub-and-spoke 拓扑图

- **设计**：改成和 `FlowDiagram`（演示区域）同一套暗色终端视觉语言——中心 `AgentRuntime` 胶囊节点，向下用树形连接线（主干 → 横向汇流线 + 4 条支线）分别连到 4 个入口节点；入口节点直接复用 `SurfaceIcon.tsx` 的真实品牌图标（飞书/Telegram/Discord 官方 SVG + TUI 终端图标），而不是纯文字。汇流线上叠加一条常驻的流动光效（复用已有的 `flowPulse` keyframe）。核心节点和入口节点都有入场动效（缩放淡入 / 交错上滑淡入），尊重 reduced motion。
- **踩的坑（供以后同类改动参考）**：
  1. 最初用一个 `<svg viewBox="0 0 100 62" preserveAspectRatio="none">` 画连接线，实测发现**非等比缩放会把 `stroke-dasharray` 的虚线段严重拉伸变形**（横向拉伸约 8 倍、纵向约 5.8 倍，虚线段变成一坨坨色块），而且线条颜色只有 16% 透明度白色，在截图里几乎完全看不见——两个问题叠加导致连接线实质上"不存在"。改成纯 CSS 定位的 `<span>` 元素（主干/汇流线/支线各自 `position:absolute` + `top/bottom/left/right` 精确计算），彻底避开 SVG 非等比缩放的坑，颜色也提到 30% 透明度、线宽 1.5px 保证可读。
  2. Framer Motion 的 `animate={{ y: ... }}` 会接管元素的 `transform` 属性，**和 CSS 里写的 `transform: translateX(-50%)` 会冲突**（谁的 transform 生效取决于内联样式覆盖顺序，实测 CSS 的会被 Framer 直接吃掉）。改成把居中偏移也交给 Framer 自己管：`style={{ left: '12%', x: '-50%' }}`，让 Framer 把静态的 `x` 和动画的 `y` 合并进同一个 transform。
  3. **最隐蔽的一个**：改完背景色一直不生效，实测 `getComputedStyle` 发现 `.channels-map` 的背景色是浅色 `rgb(247,249,252)`，不是我写的 `var(--terminal)`。根因是 `MarketingHome.module.css` 里有一条 `.root :global(.runtime-map), .root :global(.channels-map), .root :global(.safety-map), .root :global(.memory-map), .root :global(.automation-map) { background-color: #f7f9fc; ... }`——CSS Module 的 `:global()` 选择器因为多了 `.root` 祖先类，**优先级天然比 `globals.css` 里的同名单类选择器高**，不管源码顺序谁在后面都会赢。这是本轮会话里第三次踩到同一类型的坑（前两次是 `.evidence-strip` 和 `.runtime-map`），说明这个 module CSS 文件里还有大量"影子样式"，以后每次重做一个 `*-map` 组件之前，应该**先搜一遍 `MarketingHome.module.css` 里同名的 `:global(...)` 规则**，而不是等改完发现不生效再排查。已经把 `.channels-map` 从这条共享浅色背景规则里摘出来，单独给了一条深色边框规则；顺带删掉了两条只会匹配旧 `> div` 结构（现在改成了 `> li`）、永远不会再命中的死规则（`nth-child` 描边色、hover 效果）。
- **可视化验证**：Playwright + 系统 Chrome 截图确认，中心节点、四条品牌图标入口节点、树形连接线（含流动光效）全部按预期渲染；和下方演示区域的暗色面板视觉语言一致。
- **文件**：`website/src/components/marketing/capabilities/ChannelsPanel.tsx`、`website/src/styles/globals.css`、`website/src/components/marketing/MarketingHome.module.css`

### 验证

- `npx tsc --noEmit`：通过
- `npx eslint src/`：通过
- `npx vitest run`：28/28 通过

### 待办

`安全边界` / `记忆` / `自动化` 三个能力面板还没做。用户中途提出一个新的更大范围的需求（项目改名，仓库地址换成 NEDONION/lobster0，需要同步改 README 和官网），本轮时间优先处理改名调研，这三个面板的重做顺延到改名之后。
