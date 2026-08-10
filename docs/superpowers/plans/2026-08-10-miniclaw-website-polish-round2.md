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
