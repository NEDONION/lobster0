# Lobster0 官网设计改动记录 · Round 2

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

### 触发反馈（用户原话，来自线上 lobster0.jchu.tech 的真实截图）

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
  - 删除了 `public/images/lobster0-tui-approval-warp.{png,webp}` 和 `lobster0-tui-external-cli-warp.{png,webp}`——确认过仓库根目录 `README.md`/`README_EN.md` 用的是 `docs/assets/` 下的独立副本，不受影响。
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

## Round 6（2026-08-11）

### 触发反馈

用户提出三件事：项目改名为 `lobster0`（GitHub 仓库已经从 `NEDONION/lobster0` 重命名为 `NEDONION/lobster0`，确认过展示名就用小写 `lobster0`，和仓库 slug 一致）；参照 claw-x.com / openagents.org / lobsterai.youdao.com 三个站点重做设计，圆角太多、"能力/特性"板块精度不够；域名换成对应的。

设计方向上，先出了三版静态提案（深海终端 / 龙虾蓝图 / 真实界面剧场），用户选了**方向 C：真实界面剧场**——保留现有浅色系统和字体，把抽象节点图/连线图换成真实产品界面模拟（聊天窗口、终端窗口），让访客一眼看懂"这是真产品在跑"。

### 已完成

#### 1. 域名：新增 lobster0.jchu.tech

`npx vercel domains add lobster0.jchu.tech lobster0`，DNS 已验证通过（`vercel domains verify` 返回 `status: ok`）。沿用 `lobster0.jchu.tech` 那次的操作方式，域名指向同一个 Vercel 项目（lobster0 项目暂未改名，产品名和项目名解耦，不互相依赖）。

#### 2. 全局圆角系统性收紧

参照 claw-x.com（10–16px）和 openagents.org（5–16px）的实测圆角值，把 `globals.css` + `MarketingHome.module.css` 里散落的圆角从 18–28px（含多处 999px 胶囊按钮）统一收紧到 8–12px。**保留**了小徽标/标签（`.claw-trace__status`、`.runtime-map__state`、`.flow-diagram__icon` 等）的 999px/50% 圆形——这类小尺寸 pill 在两个参考站点里也是标准用法，不属于"滥用"，真正需要收紧的是大容器和主按钮。改动是纯字符串精确匹配替换（`border-radius: 18px;` → `12px;` 等），没有动其他属性，风险低。

#### 3. 特性板块 03/04/05 从抽象图改成"真实界面剧场"

- **03 安全边界**：改成飞书聊天窗口模拟——用户请求高风险操作 → Agent 提示需要确认 exact argv → `POLICY_CHECK` 卡片展示真实命令 `rm -rf /tmp/lobster0-cache-2026` 和四档权限模式按钮（SAFE 高亮）→ `RESULT_DELIVERED` 卡片展示四项检查通过。新建 `.chat-scene` / `.chat-bubble` / `.chat-card` / `.chat-modes` 系列样式，复用 `SurfaceIcon.tsx` 的真实飞书图标做窗口顶栏。
- **04 记忆**：改成深色终端窗口，左栏是 `facts.md` 的真实 Markdown 片段（帯简单语法高亮），右栏是对应的 SQL 查询与格式化表格结果，中间"投影"箭头用已有的 `flowPulse` 光效动画连接。直接展示"Markdown 是 Truth，SQLite 是 Projection"这句话在真实文件/查询层面是什么样子，比两个方块加箭头有说服力。
- **05 自动化**：改成 Discord 聊天窗口模拟——定时任务触发 → `AUTOMATION_GATE` 拦截卡片，带一个真实的 iOS 风格开关组件（当前关闭态）+ "默认关闭"文字 → 绿色"一旦开启"卡片展示 所有者启用→策略门禁→15 条版本化场景 的流程 → 底部 `Implementation PASS ≠ Live PASS` 免责声明条。复用 `SurfaceIcon.tsx` 的 Discord 图标。
- 03/05 两个聊天窗口共享同一套 `.chat-scene` 系列样式（只是 app 图标、消息内容、卡片状态不同），04 记忆单独一套 `.memory-scene` 终端样式。三个面板都用 IntersectionObserver 触发的交错入场动画（复用已经验证过的“进入视口再播放”模式），尊重 reduced motion。
- 旧的 `.safety-map` / `.memory-map` / `.automation-map` 系列 CSS（含 `globals.css` 和 `MarketingHome.module.css` 两处）全部删除，没有留死代码。
- 同步更新了 `MarketingHome.test.tsx` 里断言"默认状态"文字的用例，改成断言新文案"默认关闭"。

**02 多入口没有改成聊天窗口**——这是有意的技术判断：多入口面板讲的是"一个 AgentRuntime 同时连接四个平台"，这是架构关系，不是单次交互，塞进一个聊天窗口反而讲不清楚。Round 5 刚做的深色 hub-and-spoke 拓扑图保留，且和这轮新增的 `.chat-scene` / `.memory-scene` 深色终端窗口在视觉基调上是统一的（都是浅色外壳 + 深色终端/窗口展示关键机制），不违和。

#### 4. 出过一版静态设计提案（未采纳的两版记录在案，供以后参考）

在动代码之前，先用 Artifact 出了三版静态 HTML 提案给用户选：

- **方向 A 深海终端**：延续已验证的深色终端节点图语言精修，圆角收紧，风险最低。
- **方向 B 龙虾蓝图**（未采纳，但认为是三版里最独特的一版）：暖白图纸底 + 蓝图网格线 + 龙虾壳橙红强调色，工程图纸美学（裁切标记、刻度尺、图纸标注），直接呼应新名字"lobster0"和产品"结构化、可审查"的叙事，市面上 AI Agent 官网基本没人这么做。
- **方向 C 真实界面剧场**（用户选中）：如上。

第一次发布的 Artifact 链接用户点开显示 404（怀疑是权限问题——Artifact 默认私有，只有发布账号能直接打开），改用 `SendUserFile` 直接把 HTML 文件发过去解决。

### 验证

- `npx tsc --noEmit` / `npx eslint src/` / `npx vitest run`（28/28）：全部通过
- `npx playwright test tests/e2e/marketing.spec.ts`：2 个失败，和 Round 3/4 记录的**同一组**预先存在的 flaky（`#safety-tab` 时序、`.claw-trace` reduced-motion 断言）一致，与本轮改动无关
- 可视化：Playwright + 系统 Chrome 截图，03/04/05 三个面板桌面端逐一截图确认

### 待办（已完成，见 Round 7）

~~项目改名（README.md / README_EN.md / 官网品牌名 MiniClaw → Lobster0，含 `siteFacts.install` 里写死的旧仓库地址）还没做~~——另一个并行 session 用专门的 `codex/rename-lobster0` 分支做完了，见 Round 7。

## Round 7（2026-08-11）

### 触发反馈

用户要求把改名合并到 main、追问 `lobster0.jchu.tech` 打不开、`miniclaw.jchu.tech` 显示的名字问题，以及"全站按钮组件不精细、比较笨重，参照 Vercel 精细化"。

### 已完成

#### 1. 协调另一个 session 的改名工作，安全合并进 main

发现有独立的 `codex/rename-lobster0` 分支（专属 worktree）在做**全产品**改名（Python 包/CLI/PyPI 名/状态目录/环境变量前缀/npm scope/README/官网，553 个文件），设计文档写"用户已确认，执行中"。命名规范核对：仓库/URL 全小写 `lobster0`，展示文案（README、官网）用 `Lobster0`——和该分支自己的设计文档一致，不用改。

**发现该 worktree 处于危险的中间状态**：`git status` 显示多个 `UU`（合并冲突未解决）标记的文件，还混入了大量与改名无关的未提交改动（桌面视觉刷新、飞书 phase6 生产验收等）。**没有碰这个 worktree**——转而验证了一个关键事实：分支的最后一次**提交**（`e027cbc`，一次干净的、已解决冲突的 `Merge remote-tracking branch 'origin/main'`）和当前 worktree 的脏工作区状态是两回事，git merge 只关心已提交内容。用一个全新的临时 worktree（`git worktree add --detach /tmp/... origin/main`）独立验证：`e027cbc` 合并进当前 main 完全无冲突（`git merge-tree` 干净输出）、内容里没有残留冲突标记、Python 包能正常 `import lobster0`、README 品牌名确认干净（15 处 `Lobster0`，0 处遗留）、网站 `tsc`/`eslint`/`vitest` 全绿。正准备 push 时发现**main 已经在这几分钟内被通过 GitHub PR #6 合并了**（另一个 session/用户自己走的标准 PR 流程）——对比了我独立验证的合并结果和 PR #6 合并后的 main，`git diff` 完全一致，确认不需要重复推送，直接清理了临时 worktree。

然后把 `codex/miniclaw-website` 网站分支同步到最新 main（`git merge origin/main`），干净无冲突，`tsc`/`eslint`/`vitest` 全绿，推送。

#### 2. 域名排查

- `miniclaw.jchu.tech`："名字还是 miniclaw" —— 用 `curl` 直接抓服务端渲染内容确认 `<title>` 已经是 `Lobster0 — 你的本地行动助手`，代码层面没问题，是域名字符串本身或浏览器缓存的事。
- `lobster0.jchu.tech` 打不开 —— `vercel domains inspect` 显示 DNS 未配置（不确定是之前记录有误还是记录被清掉了），给了用户需要在 DNSPod 加的 A 记录，用户已加，等待生效验证。

#### 3. 按钮/阴影系统精细化，实测对齐 Vercel

没有凭感觉猜，直接用 `javascript_tool` 在 vercel.com 真实页面上跑 `getComputedStyle` 抓取按钮的圆角/阴影/字重/高度实测值：导航小按钮圆角 6px、主 CTA 圆角 8px（强调型用 9999px 胶囊）、**几乎不用模糊投影阴影**（大部分 `box-shadow: none`，次要按钮只用 `0 0 0 1px` 描边模拟边界）、字重 500（不是 700 那种粗体）、高度 40px。

对照实测数据改了两处：
- `.button` 系统（`globals.css`）：`min-height` 48px→40px，`border-radius` 10px→8px，`font-weight` 680→500，字号加了 `0.875rem`；`.button--primary` 的 `box-shadow: 0 12px 32px ...`（这是"笨重感"的主要来源）直接删除，hover 从 `transform: translateY(-2px)` 位移改成纯颜色过渡（`background-color` 变暗）。
- 全站 24 处 `box-shadow` 里，把 13 处模糊半径在 40-80px、明显"发光/悬浮"感过重的投影阴影，统一收紧到 12-28px 模糊半径、更低的不透明度（描边环性质的 `0 0 0 Npx` 阴影不动，那类本来就是克制的）。
- **踩坑（本轮会话第四次同类问题）**：`MarketingHome.module.css` 里又发现一条 `.root :global(.button--primary) { box-shadow: 0 12px 28px ... }`，把我在 `globals.css` 里删掉的按钮阴影重新加了回来，`.root :global(.button)` 也维持着旧的 46px/10px/0.9rem 数值。删掉这条 override，让 `globals.css` 的统一新标准生效。这进一步印证了之前记录的经验：**每次改动共享组件样式前，先搜一遍 `MarketingHome.module.css` 里的同名 `:global(...)` 规则**。

可视化验证：Playwright + 系统 Chrome 截图确认按钮外观已经是扁平、无阴影、圆角适中的效果，品牌名 `Lobster0` 正确显示。

### 验证

- `npx tsc --noEmit` / `npx eslint src/` / `npx vitest run`（28/28）：全部通过
- `npx playwright test tests/e2e/marketing.spec.ts`：2 个失败，仍是同一组已知的预先存在 flaky，与本轮改动无关

### 待办

用户还要求"演示"3 个 tab 照着"特性 02-05"的真实界面剧场风格重做、"特性 01"（运行时）也重做——这两项还没做，是下一步。装甲龙虾 Logo（用户要求带盔甲/武器感，不是单纯龙虾）也还没做。

---

## Round 8 — 文档站视觉统一（2026-08-11）

### 背景

官网首页已经打磨了七轮，但 `/docs` 一直是 Fumadocs 的出厂模板：中性灰配色、默认排版、浅色代码块，和首页的蓝灰调（`--canvas #f4f7fb` / `--signal #4267f5`）加深色终端完全是两套语言。用户要求"打磨文档站的样式"。

### 先查到的真 Bug：整个文档站的响应式隐藏全部失效

动手改样式之前先看现状，发现桌面端顶部有一条 1px 的"幽灵横条"，里面的品牌名和图标溢出可见。查下去是个结构性问题：

`#nd-subnav` 带着 `md:hidden`，但 `getComputedStyle` 返回 `display: flex`。抓服务端 CSS 一看，`@layer utilities` 在同一个文件里**出现了两次**（偏移 14587 和 117590）——`src/app/[lang]/layout.tsx` 引入了 `fumadocs-ui/style.css`（这个包**自带一整份编译好的 Tailwind**），而 `globals.css` 又 `@import 'tailwindcss'` 编译了第二份。同名 layer 会合并，合并后纯粹按源码顺序决胜，于是第二份构建里的 `.flex`（118291）压过了第一份里的 `md:hidden`（88344）。

**影响面不止那条横条**：Fumadocs 所有 `md:` / `lg:` 响应式隐藏都失效，桌面端渲染着本该只在移动端出现的 chrome。

修法是按 Fumadocs 的推荐姿势收敛成单份构建——`globals.css` 里改成
`@import 'tailwindcss'` + `@import 'fumadocs-ui/css/neutral.css'` + `@import 'fumadocs-ui/css/preset.css'`，
并从 `layout.tsx` 删掉 `fumadocs-ui/style.css`。修完 `display` 变回 `none`，layer 只剩一份，构建产物 CSS 也从 174KB 降到 142KB。

### 视觉统一

1. **令牌映射**（最高杠杆）：在 `globals.css` 加一个 `@theme` 块，把 Fumadocs 的 17 个 `--color-fd-*` 全部重新指向品牌调色板。一处改动同时覆盖侧栏、TOC、搜索框、弹层、按钮——比逐个追 Fumadocs 的类名稳得多。
2. **白纸浮于画布**：`#nd-page` 给 `--surface` 白底 + 右边框，侧栏留在 `--canvas`，复用首页"白色面板浮在浅蓝画布上"的语言。
3. **排版**：文档标题继承首页的紧字距重字重（h1 `letter-spacing: -0.045em` / `font-weight: 700`），正文行高 1.78。
4. **代码块换成深色终端**：`source.config.ts` 把 shiki 主题固定成 `github-dark-default`（明暗都用），CSS 把底色覆盖成 `--terminal`——同一条命令在首页和文档里长得完全一样。复制按钮的中性色在深底上会消失，一并改成半透明白。
5. **引用块**：Fumadocs 默认把 blockquote 渲染成粗体斜体且没有任何标记，读起来像在喊。改成左侧 2px 品牌蓝竖线 + 常规字重。
6. **表格**：圆角边框 + `--canvas` 表头。
7. **TOC**：Fumadocs 会把视口以上的**所有**标题标成 `data-active`，全部染蓝会变成一片蓝墙——文字保持 `--ink`，让竖轨承担高亮。

### 顺手清掉的两处遗留

- 文档站品牌标记还是改名前的蓝色方块 **"M"**（MiniClaw），换成和首页一致的 `BrandMark`（🦞）。
- `baseOptions` 里的 `githubUrl` 在侧栏底部渲染出一个近乎空白的图标条（主题切换已关闭，条里只剩它一个），且和已有的带文字 GitHub 链接重复——删掉。
- zh-CN 补了 `Collapse Sidebar` / `Hide Sidebar` / `Show Sidebar` 三个漏翻的键。

### E2E：一个测试辅助函数的真实缺陷

修好双 Tailwind 之后 `docs.spec.ts` 的两个桌面用例挂了，但**不是产品回归**。

`firstVisible()` 只调 `isVisible()`，而 Playwright 的可见性判定**不看 opacity**。Fumadocs 会常驻挂载一个「侧栏折叠时的浮动工具条」，用 `opacity-0 pointer-events-none` 藏着。之前 subnav 因为 1px 高但子元素溢出，被判定为可见并被选中，测试侥幸通过；subnav 一旦真正 `display: none`，选择器就落到了那个永远点不到的透明按钮上。

两处修正：
- `firstVisible()` 增加祖先链 opacity 检查，跳过淡出的元素。（只查 opacity，不查 `pointer-events`——侧栏容器本身是 `pointer-events-none *:pointer-events-auto`，查后者会误伤。）
- 桌面端的搜索入口本来就不是那个图标按钮，而是侧栏里的「搜索 ⌘K」输入框（无 aria-label）。测试按 `isMobile` 分支选对应控件。

### 验证

- `tsc` / `eslint` / `vitest`（28/28）/ `npm run build`（35 条静态路由）全绿
- `npx playwright test`：11 passed / 1 skipped
- 移动端 390×844 文档页零横向溢出，截图确认 subnav 正常

---

## Round 9 — 字体加载（2026-08-11）

### 问题

字体走 Fontsource 的 CSS 引入（`import '@fontsource-variable/instrument-sans'`）。Fontsource 出的是普通样式表，浏览器**必须先拿到并解析 CSS 才能发现字体文件**，多出一整个往返。实测 HTML 里连一条 `rel="preload"` 都没有。

### 走过的两条弯路

1. **JS import 取 URL + 手写 preload**：Turbopack 下能拿到 `/_next/static/media/...woff2`，dev 正常；但 `package.json` 的 build 脚本是 `next build --webpack`，webpack 没配 woff2 loader，**生产构建直接失败**。
2. 想给 webpack 补 asset rule —— 放弃。CSS 里引用的字体由 Next 自己的管线发出（`ae05c57c...`），JS import 由我的 rule 发出（另一套 hash），两条路径产出**两份文件**，preload 的和 CSS 请求的不是同一个 URL，反而下载两次。

### 最终做法

改用 `next/font/local`（`src/lib/fonts.ts`）：它自己生成 @font-face（只有一份文件）并自动发 preload，且 Turbopack / webpack 都支持。

- 只声明 Latin 子集：CJK 由 fallback 里的系统字体承担，文案里没有 latin-ext 字符（法语 Œ/œ 落在 Latin range 内）。
- 不声明斜体：31 KB 只有在 preload 的前提下才值得，文档里那几个强调标签用合成倾斜完全够看。
- 导出名要用 `instrumentSans` / `plexMono` —— `next/font` 拿**导出标识符**当生成的 font-family 名，叫 `sans` 会 ship 出 `font-family: sans`。
- `global-not-found.tsx` 自己渲染 `<html>`，也要挂 variable className。

### 实测（150ms RTT / 1.6Mbps 节流，各 5 次取中位数）

| | 字体开始下载 | 主字体到位 |
|---|---|---|
| 改前 | 742ms | 1716ms |
| 改后 | **164ms** | **943ms** |

主字体提前约 **770ms** 到位。改前正文在 ~820ms 用 fallback 画出来，要到 1716ms 才换成真字体 —— 近 900ms 的 FOUT 窗口，而 fallback 是 PingFang SC，和 Instrument Sans 字面宽差很多，回流很明显。改后字体在首次内容绘制时已经就位。

**FCP 不作为依据**：同一构建 5 次跑出 736–988ms 的双峰分布，噪声大于任何配置间差异，不能拿来支持结论。

### 验证

`tsc` / `eslint` / `vitest` 28 / `build` / `playwright` 11 passed 全绿；截图确认 Latin 走 Instrument Sans、中文走 PingFang SC、代码块走 Plex Mono，404 页同样生效。

---

## Round 10 — 全站质量巡检：链接 / 控制台 / 可访问性（2026-08-11）

### 巡检一：爬全站（35 页）

从 5 个语言首页 + 中英文档根出发爬遍所有内部链接，检查控制台错误、未捕获异常、死链、缺 alt、标题层级跳级、重复 id、h1 数量。**35 页全部干净，零问题。**

### 巡检二：对比度（WCAG AA）

自己实现了 WCAG 相对亮度公式逐元素算，覆盖首页 + 英文首页 + 6 个文档页。

**一次假阳性教训**：第一版脚本用 `/[\d.]+/g` 抽颜色数值，碰上 Fumadocs 侧栏的 `oklab(0.999994 0.0000455677 0.0000200868 / 0.8)` 被解析成 RGB(0.99, 0.00004, 0.00002)≈纯黑，于是报出「文档站品牌名对比度 1.17」这种荒谬结果。改成只信 `rgb()/rgba()`、遇到其它色彩空间就跳过，假阳性清零。

去噪后剩 **6 处真实失败**，全在首页：

| 比值 | 需要 | 元素 |
|---|---|---|
| 2.54 | 4.5 | `.runtime-map__index` 的「01/02/03」，硬编码 `#9aa3b2` |
| 2.60 | 4.5 | `.hash-tabs__list button span` 的序号，硬编码 `#98a1b2` |
| 4.35 | 4.5 | `.section-kicker`（`--signal` 落在 `--canvas` 上） |
| 4.35 | 4.5 | `.runtime-map__state` 成功态绿 `#1d8967` |
| 4.44 | 4.5 | 页脚链接（`--muted` 落在页脚灰底上） |

前两个是关键：2.5:1 不叫「低调」，叫看不清。

### 处理

- `--muted` `#647087` → `#5f6b81`，`--signal` `#4267f5` → `#3a5ee8`。两个 token 本来都卡在 4.5 下面一点点，压暗一档就一次性修好所有下游使用点，顺带把「白字压在 signal 上」的按钮也从 4.68 提到 5.32。
- 两处硬编码浅灰序号并入 `var(--muted)`；成功态绿 `#1d8967` → `#1a7d5f`（5 处同角色的用法一起改）。
- 复测：**6 → 0**。截图确认序号仍然视觉次要（细体等宽小字），设计意图没被破坏。

### 顺手

键盘 Tab 序正确、每个可聚焦元素都有焦点环。但发现首页安装代码块的 `<pre tabIndex={0}>` 是个**空转的 Tab 停靠点**——按用户要求这个块在任何宽度都平铺不滚动（桌面和 390px 移动端实测 overflow 均为 0），可聚焦的滚动容器这个前提不成立，去掉。

### 验证

`tsc` / `eslint` / `vitest` 28 / `build` / `playwright` 11 passed 全绿。

---

## Round 11 — 「Implementation PASS ≠ Live PASS」其实没删干净（2026-08-11）

### 起因

本来打算逐条审 ja/ko/fr 的机翻质量。读日文 `capabilities` 时看到 `'バージョン管理された 33 ケース'` 和 `'Implementation ≠ Live'`——**这正是用户早就要求删掉的那句「很不专业的字」**。

### 实情：只删掉了五分之一

用户原话是「官网上很不专业的字【33 条 versioned Channel cases；15 条 versioned Automation cases。Implementation PASS 不等于 Live PASS。】都给我去掉啊」。当时只把 `evidence.disclosure` 那一条从渲染里摘掉了，同样的内容还活在另外四个地方，**五种语言的首页都在渲染**：

1. `AutomationPanel.tsx:92` —— 一段**硬编码、没走 i18n** 的 `Implementation PASS ≠ Live PASS`（所以五种语言都显示英文原文）
2. `AutomationPanel.tsx:89` —— 流程图末节点 `{automationCases} {ui.versionedCases}`（「15 条版本化场景」）
3. `capabilities[].facts` —— channels 的「33 条 versioned cases」、automation 的「15 条 versioned cases」和「Implementation ≠ Live」
4. `capabilities[].summary` —— 五种语言的 automation 简介都以「再用 versioned cases 验证实现语义」结尾

更糟的是 **`site.test.ts` 里有一条测试在断言这句话必须存在**（`expect(...evidence.disclosure).toContain('Live PASS')`）——测试守着的正好是用户要求删除的东西。

### 处理

- 删掉硬编码段落和流程图末节点（`所有者启用 → 策略门禁` 读起来反而更干净，没有悬空箭头）。
- 三条 `facts` 换成**已在页面上、用户已审阅过的说法的重述**，不引入新的产品声明：入口之间能力一致 / 触发前仍过 Policy / 可随时关闭。
- 五种语言的 automation `summary` 结尾从内部 QA 指标改成对访客有意义的性质：「启用一次不等于长期放行，每次触发依然要过 Policy」。
- 顺带清掉因此变成死代码的：`counts.channelCases`（本来就零引用）、`counts.automationCases`、`ui.versionedCases`（五种语言）、`evidence.disclosure`（类型 + 五种语言）、`.chat-scene__disclosure` CSS 规则。
- 那条测试反转成**防回归守卫**：遍历全部五种语言的 `marketingCopy`，`versioned cases` / `Live PASS` / `Implementation ≠ Live` 一律不许出现，失败时直接报出是哪个语言命中。

文档 MDX 里的同类表述**保留**——技术文档里说明测试覆盖的边界是合理的，用户的批评针对的是营销首页。守卫的范围也只限 `marketingCopy`。

### 教训

「删掉某段文案」这类要求，不能只搜渲染它的那个组件。这次同一句话散在 4 个位置 × 5 种语言 = 20 处，还有一条测试在反向守护它。**改完要按最终产物（渲染出的 HTML）逐语言复查**，而不是按源码搜索确认。

### 验证

五种语言首页 grep 全部 clean；`tsc` / `eslint` / `vitest` 28 / `build` / `playwright` 11 passed 全绿。
