# 桌面版视觉重做：从「配色练习」到「生产级应用」

> 日期：2026-08-14
> 文档类型：视觉与前端设计（**写于实现之前**）
> 状态：`DRAFT FOR REVIEW / IMPLEMENTATION PENDING`
> 参考实现：[netease-youdao/LobsterAI](https://github.com/netease-youdao/LobsterAI)（★5.9k）、
> [ValueCell-ai/ClawX](https://github.com/ValueCell-ai/ClawX)（★7.6k）
> 取代：`2026-08-11-desktop-visual-refresh-claude-inspired-design.md`（Claude 暖色重涂，见 §2.4）

## 1. 起因

Owner 给了三条具体反馈和一条整体判断：

> 这个箭头太小了。还有加粗和正文的字体、字重颜色 一点都不突出和显眼啊
>
> 桌面版我们的整体样式 **完全不像一个开源的生产级应用**
>
> 仿照 LobsterAI / ClawX 的桌面版前端代码去做

前三条是症状，第四条是病。这份文档先诊断病，再开药。

## 2. 现状核查：对着两个参考仓库的真实代码比

**不是凭印象比**——两个仓库的样式文件都拉下来读过，官网落地页的 token 也用
`getComputedStyle` 量过。

### 2.1 我们抄了 token 的名字，没抄让它变好看的东西

`desktop/src/renderer/theme.css` 的注释自己写着「ported from LobsterAI, theme
classic-light」。核对下来是**结构上的 1:1 移植**——`--lobster-primary` /
`--lobster-surface-raised` / `--lobster-chat-user` / `--lobster-gray-1..9` 逐个对得上。

但 LobsterAI 真正的样子是：

| | LobsterAI | ClawX | **我们** |
| --- | --- | --- | --- |
| 主题数量 | **14 套**（classic-light/dark、nord、mocha、midnight、paper、sakura…）+ 主题引擎 | light + dark（shadcn 语义 token） | **1 套，硬编码** |
| 暗色模式 | 有 | 有 | **`color-scheme: light`，没有** |
| token 契约 | `ThemeDefinition` + `SHARED_TOKENS`，types.ts 强约束 | `globals.css` + `tailwind.config.js` 两层，另有一份 token 使用规约文档 | 一份 CSS 变量表 |

**我们移植的是配色表，丢掉的是主题能力本身。** 一个只有单一浅色皮肤、还是别人家
配色改涂的应用，无论颜色调得多准，都不会像生产级产品——因为生产级产品的标志不是
"这个颜色好看"，而是**"它有一套可以被替换的体系"**。

### 2.2 三条具体反馈，逐条在代码里定位

**（a）箭头太小** —— `styles.css:791`

```css
.process-caret {
  width: 12px;
  color: var(--lobster-text-muted);   /* #8a887f，在 #faf9f5 上对比度约 3.4:1 */
  font-size: 11px;                     /* 而且是文字字形 ▸ / ▾，不是图标 */
}
```

11px 的**文字三角**加上最低一档的灰。三个问题叠在一起：小、淡、而且是字形。
`▸` 在不同字体里的字面大小和垂直位置都不一致，这是"业余感"最直接的来源之一——
两个参考仓库都用真正的矢量图标（lucide），没有一处用文字三角当图标。

**（b）加粗和正文不突出** —— `styles.css:596` 与 `theme.css`

```css
.message .markdown { font-size: var(--lobster-text-sm); }  /* 14px */
.markdown strong   { font-weight: 600; }
:root              { font-weight: var(--lobster-ui-font-weight-normal); }  /* 445 */
```

- **正文 14px**。LobsterAI 官网正文 16px；ClawX 用 shadcn 默认 16px。聊天是**长文
  阅读**场景，14px 是给密集列表用的尺寸；
- **加粗只比正文重 155 个单位**（445 → 600）。常规做法是 400 → 700，差 300。我们
  为了模仿 Claude 把基础字重抬到 445，等于**主动把加粗的对比度削掉了一半**；
- 并且 `font-synthesis: none`——中文字体没有真正的 600 字重时不合成，**中文加粗
  可能几乎不生效**。截图里的中文加粗弱正是这个原因。

**（c）颜色不突出** —— 次要文字 `--lobster-text-muted: #8a887f` 在 `#faf9f5` 上约
3.4:1，低于 WCAG AA 正文 4.5:1。步数、时间、token 数全用它。

### 2.3 两个参考仓库真正在做的事

**ClawX**（`src/styles/globals.css`，676 行）：

- shadcn 标准语义 token（`--background` / `--foreground` / `--muted-foreground` …），
  HSL 三元组不带 `hsl()` 包装，由 `tailwind.config.js` 重新组装；
- 额外三层中性表面 `--surface-modal` / `--surface-input` / `--surface-sidebar`，
  **暗色下重定向到已有 token**，不维护第二套表面色板；
- 文件头有一整块**「组件约定替换表」**：`bg-white dark:bg-card` → `bg-surface-modal`，
  选中态一律 `bg-black/5 dark:bg-white/10`，并注明参考实现文件。

最后这条是关键：**它把"怎么用 token"也写成了规约**，所以几十个组件才不会各写各的。
我们没有这层，于是 `styles.css` 1856 行里同一个语义有多种写法。

**LobsterAI**：`ThemeDefinition` 类型 + `SHARED_TOKENS` 共享默认值，每套主题只覆盖
差异项。`classic-light` 是冷调中性（`#F8F9FB` 背景、`#0D0D0D` 前景、`#3B82F6` 蓝），
`classic-dark` 是 `#0F1117` / `#E4E5E9`。

### 2.4 为什么推翻 8-11 那份「Claude 暖色重涂」

那份文档的目标是"像 Claude"。事后看，它选错了对标对象：

- Claude Desktop 是**闭源商业产品**，它的视觉语言服务于品牌；我们是开源工具，
  用户期待的是"看起来可信、可改、可主题化"；
- 暖米色（`#faf9f5` / `#875645`）在长文阅读下**降低了对比度预算**——暖底色让同样的
  灰看起来更浑；
- 最重要的：**照着截图调颜色，学不到体系**。这次改为照着两个开源仓库的**代码**做，
  能连规约一起学。

## 3. 方案

### 3.1 A. 回到中性色板，品牌色只出现在该出现的地方

采用 ClawX/shadcn 那一路的**冷调中性**作为表面与文字，理由是中性底色把对比度预算
全部留给内容。品牌色（龙虾橙）**不做通用 accent**，只用于：品牌标识、主按钮、
以及需要"这是本产品的动作"的极少数位置。

主按钮采用官网落地页量到的做法——**近黑填充 + 全圆角**（实测 `rgb(21,23,28)`、
`border-radius: 9999px`、`font-weight: 600`），而不是大面积橙色。橙色作为大面积
填充在深色模式下几乎必然刺眼，近黑/近白在两种模式下都稳。

### 3.2 B. 补上暗色模式（这是"生产级"最大的单项信号）

`color-scheme` 改为 `light dark`，token 分三处定义：

1. `:root` 定义**完整浅色**全量 token；
2. `@media (prefers-color-scheme: dark)` 下用 `:root:not([data-theme="light"])` 覆盖；
3. `:root[data-theme="dark"]` 再覆盖一次，让显式选择在两个方向上都赢过系统。

**不做 14 套主题**。LobsterAI 的主题引擎需要运行时注入与持久化，是独立一块。
先把"两套皮肤 + 一个能被替换的 token 契约"做扎实——**颜色不得写死在组件里**，
这条守住了，未来加主题只是多几组变量。

### 3.3 C. 排版：把对比度还给内容

| 项 | 现在 | 改为 | 理由 |
| --- | --- | --- | --- |
| 对话正文 | 14px | **15px** | 长文阅读；不直接跳 16px 是因为侧栏/面板是密集布局，15px 是两者的平衡点 |
| 基础字重 | **445** | **400** | 445 是为模仿 Claude 引入的非常规值，代价是削掉加粗对比 |
| 正文加粗 | 600 | **700** | 400→700 差 300；且 700 是中文字体真实存在的字重，不依赖合成 |
| 标题 | 600 | **700** | 同上 |
| `font-synthesis` | `none` | **`weight`** | 中文字体缺 700 时允许合成，宁可略糙也不要"加粗看不出来" |
| 次要文字 | `#8a887f`(3.4:1) | 提到 **≥4.5:1** | WCAG AA；步数/时间/token 数全靠它 |

### 3.4 D. 图标：文字字形一律换成矢量

`▸ / ▾` 换成 **16px 的 inline SVG chevron**，颜色跟随文字而不是最淡的灰，并给
`transform: rotate(90deg)` 的展开过渡（受 `prefers-reduced-motion` 约束）。

**不引入图标库依赖**。目前需要图标的位置屈指可数，内联一个 `<Icon>` 组件比
新增一个运行时依赖划算；等到超过 ~15 个图标再考虑。

### 3.5 E. 补一份 token 使用规约（学 ClawX 那块注释）

在 `theme.css` 顶部写清楚**替换表**：哪些语义该用哪个 token、选中态/悬停态/状态色
的统一写法。没有这层，1856 行 CSS 会继续各写各的。

## 4. 不可放宽的边界

- **组件里不得出现字面颜色值**，一律走 token。这是未来能加主题的唯一前提；
- 暗色不是"把浅色反过来"——表面层级在暗色下靠**提亮**（`#0F1117` → `#1A1D27` →
  `#242830`）而不是加阴影；
- 不复制任何一方的**品牌资产**（logo、专有字体、插画）。本次借鉴的是 token 组织方式
  与开源配色数值，两个仓库均为开源许可；
- `prefers-reduced-motion` 与 `:focus-visible` 的现有规则不得回退。

## 5. 分阶段

| 阶段 | 内容 | 为什么这个顺序 |
| --- | --- | --- |
| **P1** | 排版 + 对比度 + 图标（§3.3、§3.4） | 直接消掉 Owner 报的三条症状，改动面小、风险低 |
| **P2** | 中性色板重涂 + token 规约（§3.1、§3.5） | 需要逐个组件核对，改动面最大 |
| **P3** | 暗色模式（§3.2） | 必须建立在 P2 的 token 纪律之上；P2 没做完就做 P3 等于写两遍 |

**P1 先落地并交付**，不等整体做完——症状每天都在硌着人。

## 6. TDD 起点

CSS 本身不适合单测，测**可断言的结构与契约**：

- `theme.css` 里 `:root` 与暗色块**定义的 token 名完全一致**（漏一个就是暗色下的
  透明/黑块，这类 bug 靠肉眼很难全覆盖）；
- 组件源码中**不出现字面 hex 颜色**（扫 `desktop/src/renderer/*.tsx` 与 `styles.css`
  的非 token 区域）；
- `process-toggle` 渲染出的是 `<svg>` 而不是 `▸` 字符；
- 折叠按钮的 `aria-expanded` 在两种状态下都正确（现有行为不得回退）；
- 关键前景/背景组合的对比度 ≥ 4.5:1，用一个小的对比度计算函数在测试里断言。

最后一条是本次唯一新增的测试工具函数——把"设计意图"变成可执行的断言，比在文档里
写一句"注意对比度"有用得多。

## 7. 退出条件

1. 「过程 N 步」的箭头是 16px 矢量图标，Owner 一眼能看见；
2. 对话里加粗与正文有**明显**区别（中文也是）；
3. 次要文字对比度 ≥ 4.5:1，且有测试断言；
4. 暗色模式下没有任何"白底黑字漏进来"的区域（P3 的退出条件）；
5. `pnpm test` / `typecheck` / `build` 全绿。

## 8. 明确不做

- LobsterAI 那样的 14 套主题与运行时主题引擎；
- 引入图标库依赖（lucide 等）；
- 引入 shadcn/Radix 组件体系——那是把整个渲染层重写，与"改样式"不是一回事；
- 动 Python Core、Bridge 协议、IPC。本次改动全部在 `desktop/src/renderer/`。
