# MiniClaw Desktop 视觉基准迁移：从 LobsterAI 改为 Claude 风格

> 日期：2026-08-11
> 文档类型：Phase D1 视觉基准调整（不改变 D1/D2 已定的信息架构）
> 状态：`DRAFT FOR REVIEW / IMPLEMENTATION PENDING`
> 上位文档：[D1 打开即聊设计](2026-08-10-desktop-d1-conversation-shell-design.md)（`IMPLEMENTED`，视觉章节 §5 被本文档取代）

## 1. 触发原因

用户在真实使用 D1 之后明确反馈：不喜欢当前 LobsterAI classic-light 的冷蓝灰配色，要求参照 `ClawX`
（[github.com/ValueCell-ai/ClawX](https://github.com/ValueCell-ai/ClawX)，同类"给 AI Agent 做桌面壳"的
开源产品）、Claude 官方产品视觉，并最终明确拍板："算了你就直接模仿 Claude 吧"。

本文档只调整**视觉皮肤**（色板、字体、圆角、阴影），不改变 D1/D2 已经确定的信息架构（左栏合并导航、
打开即聊、Composer 结构、D2 四控件方案）。

## 2. 参照证据

### 2.1 ClawX（实拍截图，来自其仓库 `resources/screenshot/en/*.png`）

- 布局与 D1 现状高度相似：左栏合并"新建对话 + 历史 + 导航"，中间大 Composer，居中邀请文案——说明
  D1 当时的信息架构判断是对的，这次不需要因为参照 ClawX 而改布局；
- 暖米白背景，衬线体大标题（"What can I do for you?"／"Settings"），无衬线正文；
- 主强调色为靛蓝紫（`Add Provider` 按钮），圆角胶囊按钮和分段控件（Light/Dark/System）；
- 用户最终决定不采用这个强调色（见 §1），只保留"暖白背景 + 衬线大标题 + 圆角控件"这一层共性。

### 2.2 Claude（claude.ai 登录页，浏览器 `getComputedStyle` 实测取色，非肉眼估计）

| 项目 | 实测值 |
| --- | --- |
| 页面背景 | `rgb(252, 252, 251)` = `#FCFCFB` |
| 品牌强调色（Logo 星形图标，多次采样一致） | `rgb(217, 119, 87)` = `#D97757`（Anthropic 官方 "Claude Orange"） |
| 主文字色 | `rgb(11, 11, 11)` = `#0B0B0B`（暖黑，非纯 `#000000`） |
| 次级文字色 | `rgb(82, 81, 78)` ≈ `#52514E` |
| 更浅次级文字色 | `rgb(137, 135, 129)` ≈ `#8A8781` |
| 按钮/输入框圆角 | 统一 `10px` |
| 主 CTA（"Continue with email"） | 黑色实心（`#0B0B0B` 附近）、白字 |
| 次级操作（"Download desktop app"、"Continue with Google"） | 白底、`#0B0B0B` 文字、灰边 |
| 正文字体 | `anthropic-sans`（Anthropic 私有 Web Font，见 §3 边界声明） |
| 大标题字体 | 衬线体（同为私有 Web Font，肉眼确认是衬线，家族名未公开） |

## 3. 边界声明：参照视觉语言，不复制品牌资产

- **不下载/嵌入 `anthropic-sans` 或 Claude 的专有衬线字体**——这些是 Anthropic 的私有资产，直接引入
  既有版权风险，也违反 D1 定下的"不下载 Web Font、不增加网络依赖"原则。改用系统自带衬线字体模拟
  "衬线大标题"的观感（见 §5.2），不追求像素级一致；
- **不使用 Claude 的 Logo、"Claude" 文字标识或任何可能让用户误认 MiniClaw 是 Anthropic 官方产品的元素**
  ——只迁移色板数值、圆角、阴影这类通用视觉语言，MiniClaw 品牌标识（左上角 "M" 徽标、"MiniClaw" 文字）
  保持不变；
- 这与"模仿 Claude" 的用户诉求并不冲突：用户要的是配色和排版质感，不是品牌替换。

## 4. 新色板

延续 D1 当时的做法（LobsterAI 初版方案替换墨绿色板时）：**只换 `--lobster-*` token 的值，不改变量名**，
改动面限定在 `desktop/src/renderer/theme.css` 一个文件，不触碰任何组件结构或 class 名。

| Token | 现值（LobsterAI 冷蓝灰） | 新值（Claude 暖色系） | 依据 |
| --- | --- | --- | --- |
| `--lobster-background` | `#f8f9fb` | `#faf9f5` | Claude 实测 `#fcfcfb` 附近，取比正文卡片略深一点的暖白以保留层次 |
| `--lobster-surface` | `#ffffff` | `#ffffff`（不变） | Claude 卡片同为纯白 |
| `--lobster-surface-raised` | `#f0f1f4` | `#f1efe9` | 暖灰化 |
| `--lobster-foreground` / `--lobster-text-primary` | `#0d0d0d` | `#0b0b0b` | Claude 实测值 |
| `--lobster-primary` / `--lobster-accent` | `#3b82f6` | `#d97757` | Claude 实测品牌色 |
| `--lobster-primary-hover` | `#2563eb` | `#c1653d` | 加深版强调色，同色相降低明度 |
| `--lobster-primary-muted` | `rgba(59,130,246,0.1)` | `rgba(217,119,87,0.1)` | 同色相降低不透明度 |
| `--lobster-chat-user` | `#dbeafe`（浅蓝） | `#f4e4d9`（浅橙棕） | 用户消息气泡跟随强调色改暖 |
| `--lobster-chat-user-foreground` | `#1e3a8a`（深蓝） | `#7c3d22`（深橙棕） | 同上 |
| `--lobster-chat-bot` | `#f0f1f4` | `#f1efe9` | 暖灰化，与 `surface-raised` 一致 |
| `--lobster-text-secondary` | `#6b7280`（冷灰） | `#65635b`（暖灰） | 贴近 Claude 实测次级色 |
| `--lobster-text-muted` | `#9ca3af`（冷灰） | `#8a887f`（暖灰） | 贴近 Claude 实测更浅次级色 |
| `--lobster-border` | `rgba(224,226,231,0.6)`（冷灰） | `rgba(224,218,206,0.6)`（暖灰） | 同不透明度，色相改暖 |
| `--lobster-border-subtle` | `rgba(224,226,231,0.3)` | `rgba(224,218,206,0.3)` | 同上 |
| `--lobster-input-border` | `rgba(224,226,231,0.6)` | `rgba(224,218,206,0.6)` | 同上 |
| `--lobster-gray-1`…`--lobster-gray-9` | 冷灰阶（蓝调） | 暖灰阶（黄褐调），首尾对齐 `background`/`text-primary` | 整条灰阶改暖，保持明度梯度不变 |
| `--lobster-radius` | `0.5rem`（8px） | `0.625rem`（10px） | 对齐 Claude 实测按钮/输入框圆角 |

`--lobster-destructive`（红）、`--lobster-success`（绿）、`--lobster-warning`（橙黄）三个语义色**不变**——
错误/成功/警告必须保持可识别的独立色相，不因为强调色改橙棕就产生混淆（尤其 warning 黄橙和新 primary
橙棕肉眼接近，需要在实现时人工核对对比度和可辨识度，必要时把 warning 往黄色偏移）。

## 5. 排版

### 5.1 正文字体（不变）

`--lobster-font-sans` 保留现有系统字体优先级列表（`-apple-system, "PingFang SC", ...`），不引入
`anthropic-sans`，理由见 §3。

### 5.2 新增衬线标题字体

新增 `--lobster-font-serif` token，仅用于大标题（对话邀请文案"今天想完成什么？"、自动化/设置页
`<h1>`），系统衬线字体优先级列表：

```css
--lobster-font-serif: "New York", Georgia, "Songti SC", "STSong", serif;
```

- macOS 优先用系统衬线体 "New York"（Big Sur+ 自带，无需下载）；
- Windows/Linux 回退 Georgia（Windows 自带；Linux 视发行版可能回退到默认 serif）；
- 中文回退 "Songti SC"（macOS 自带宋体）/"STSong"，保证中文标题也有衬线质感，不出现"中文黑体+英文衬线"
  违和的半衬线效果。

应用范围严格限定为大标题（`.conversation-invite h2`、`.intro h1`、`.workspace-header` 不受影响的
`<h1>`/`<h2>` 级别），正文、按钮、表格、Markdown 正文内容**不使用衬线体**，保持可读性。

## 6. 组件方案

- **主 CTA（发送按钮）**：改为黑色实心（`background: var(--lobster-foreground)`，白字），对齐 Claude
  登录页"Continue with email"的处理——唯一、最高优先级的操作用黑色而不是强调色，强调色留给更高频、
  更轻量的状态；
- **强调色（`--lobster-primary` 橙棕）用于**：导航选中态、焦点环、链接、进度/状态点缀（如 Core ready
  绿点旁的辅助强调、思考折叠箭头 hover 态）；不用于大面积色块；
- **圆角**：统一从 8px 提到 10px（卡片、按钮、输入框、Composer 一致对齐 Claude 实测值）；
- **阴影**：不变（`--lobster-shadow-*` 现有的柔和阴影已经符合"暖白极简"的方向，不需要调整数值）。

## 7. 影响范围

- 仅修改 `desktop/src/renderer/theme.css` 的 token 值和字体新增；
- `desktop/src/renderer/styles.css` 中直接写死颜色（未使用 token 变量）的极少数规则需要排查替换为
  token 引用（若存在）；
- 新增衬线字体的应用点：`.conversation-invite h2`、`.intro h1`（非 task 视图的自动化/设置大标题）；
- 发送按钮样式（`.button-primary`）从强调色实心改黑色实心；
- 不修改任何组件结构、class 名、TSX 文件、D1/D2 已定的布局或交互逻辑。

## 8. 验证方式

- 视觉 smoke：复用 D1 已建立的模式（真实 CSS + 真实组件渲染，Browser 面板截图核对空态、消息气泡、
  折叠思考、设置/自动化页）；
- 对比度检查：新 `primary`（`#d97757`）作为按钮背景配白字、作为链接色配 `background`（`#faf9f5`）背景，
  需要满足 WCAG AA（4.5:1 正文 / 3:1 大字号），实现时用工具核实，不满足则微调明度；
- 复用 `scripts/smoke-electron.mjs` 真实 Electron + Python Bridge smoke，不需要新增断言（视觉调整不
  改变 DOM 结构和可访问性属性，现有断言应继续通过）。

## 9. 完成标准

1. `theme.css` 色板全部替换为 §4 新值，`--lobster-*` 变量名不变；
2. 大标题使用新衬线字体，正文/按钮/表格保持无衬线；
3. 发送按钮为黑色实心，强调色橙棕用于选中态/焦点/链接/点缀；
4. 错误/成功/警告三个语义色保持独立可辨识，不与新强调色混淆；
5. 圆角统一为 10px；
6. Desktop tests/typecheck/build 通过，真实 Electron + Python Bridge smoke 通过；
7. 视觉截图核对：首屏空态、消息时间线（含折叠思考）、自动化页、设置页在新配色下无断裂或对比度问题；
8. 本文档状态更新为 `IMPLEMENTED`，D1 设计文档 §5（视觉方向）标注"已被本文档取代"。

## 10. 明确非目标

- 不改变 D1/D2 已确定的信息架构、组件结构、交互逻辑；
- 不引入 Web Font 或任何需要联网下载的字体资源；
- 不复制 Claude/ClawX 的 Logo、品牌文字或专有字体文件；
- 不做深色模式（D1 当时的非目标，本次不重新引入）；
- 不因为视觉调整而重新触碰 D2（附件/模型/Workspace/Agent 控件）的功能范围。
