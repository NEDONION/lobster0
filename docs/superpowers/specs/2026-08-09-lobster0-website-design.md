# Lobster0 官网设计规格

## 1. 目标

为 Lobster0 构建一个与开源仓库同源维护的双语官网，让第一次访问者在 30 秒内理解产品，
并能在 5 分钟内进入真实安装流程。官网既是项目的营销入口，也是面向使用者的精简文档入口。

本次交付只描述和展示已经实现、已经在仓库中留下证据的能力。规划能力继续留在 GitHub 的产品、
架构与工程文档中，不得在官网伪装成当前功能。

## 2. 受众与首页唯一任务

- 主要受众：寻找可自托管个人 Agent 的开发者、开源贡献者和注重安全边界的高级用户。
- 次要受众：希望从飞书、Telegram 或 Discord 使用同一个本地 Agent 的个人用户。
- 首页唯一任务：让访问者理解“同一个本地 Core，通过多个入口行动，但所有动作仍经过统一 Policy”。

## 3. 成功标准

1. 首屏同时回答 Lobster0 是什么、为什么可信、下一步如何开始。
2. 中文首页位于 `/`，完整英文首页位于 `/en/`。
3. 中文文档位于 `/docs/`，英文文档位于 `/en/docs/`，对应页面可以互相切换。
4. 安装命令来自 `website/src/content-data/` 中的共享事实文件，首页和文档不得各自维护不同版本。
5. 首页使用真实产品截图和真实事件名称，不绘制不存在的 Web 控制台。
6. 静态构建可由 Vercel 零后端部署；Preview 经过浏览器验证后才能作为交付结果。

## 4. 信息架构

### 4.1 营销页面

- `/`：简体中文首页。
- `/en/`：完整英文首页。

首页章节顺序固定为：

1. Hero：定位、安装命令、GitHub/文档 CTA、动态 Claw Trace。
2. Evidence Rail：Python、入口、Tool、Channel Gate、Automation Gate 等可验证事实。
3. From message to action：消息如何依次穿过 Agent、Policy、Approval、Tool 和 Delivery。
4. One runtime, every surface：TUI、飞书、Telegram、Discord 共享 Core，但 Transport 与故障域隔离。
5. Powerful, with boundaries：Workspace、exact argv、SSRF、参数绑定审批和执行记录。
6. Memory that stays yours：owner-only Markdown Truth、SQLite control plane 和跨渠道 Owner Memory。
7. Real workflows：SAFE 审批与 exact-argv Git CLI 两张真实截图。
8. Quick Start：真实安装步骤、文档入口、贡献入口。

### 4.2 用户文档

- `/docs/`：从哪里开始。
- `/docs/getting-started/`：环境要求、安装、初始化、Doctor、启动 TUI。
- `/docs/runtime/`：同一个 AgentRuntime 如何服务多个入口。
- `/docs/security/`：Permission Mode、Workspace、Approval 与硬拒绝边界。
- `/docs/channels/`：飞书、Telegram、Discord 的定位与配置入口。
- `/docs/memory/`：Markdown Truth、SQLite Projection、披露边界和治理。
- 英文页面使用相同文件名并位于 `/en/docs/...`。

深入 PRD、架构、评测和发布证据不复制到网站内容中，只链接到 GitHub 对应路径。

## 5. 内容事实

官网首版只使用以下当前事实：

- Python 3.12+；默认 pi-tui 需要 Node.js 22.19+。
- 一个 Python Core、一个 OpenAI-compatible Provider、一个主 TUI。
- TUI、Feishu、Telegram、Discord 四个入口。
- 18 个内置 Tool。
- SAFE、SMART、AUTOPILOT、YOLO 四档 Permission Mode。
- 33 条 versioned Channel cases，15 条 versioned Automation cases。
- Automation 默认关闭；本地 Implementation PASS 不等同于真实平台 Live PASS。
- Secret 不进入 Prompt、普通日志或 Memory。

易变化的数字集中保存在 `website/src/content-data/project-facts.json`，中文和英文展示从同一值生成。

## 6. 视觉方向

采用已确认的 A 路线：OpenClaw 的编辑气质、OpenCode 的克制和 PicoClaw 的工程证据结构。

前端设计复核后保留深浅节奏，但不使用常见的奶油白模板：浅色区域改为冷矿物灰，强调它是运行记录纸面，
而不是生活方式品牌页面。

### 6.1 色彩

- `Ink #090B0F`：首屏与收尾背景。
- `Carbon #12161D`：Trace 与 TUI 表面。
- `Mineral #E9EDF1`：主浅色纸面。
- `Fog #D5DCE3`：浅色分隔与次级区域。
- `Signal Blue #5B72FF`：品牌、链接与活动轨迹。
- `Mint #70D6A8`：成功。
- `Amber #F2B84B`：等待审批。
- `Danger #EF615B`：拒绝与失败。

### 6.2 字体

- 标题与正文拉丁字符：Archivo Variable。
- 中文：`PingFang SC`、`Noto Sans SC`、`Microsoft YaHei` 系统栈。
- 命令、事件、数字：IBM Plex Mono。
- 极少量英文编辑式强调：Newsreader Italic。

字体通过 npm 包自托管，不依赖运行时字体 CDN。

### 6.3 签名元素

唯一主要装饰是 Claw Trace。它使用真实顺序表达运行语义：

```text
MESSAGE_RECEIVED → AGENT_PLANNING → POLICY_CHECK → APPROVAL → TOOL_EXECUTION → RESULT_DELIVERED
```

轨迹状态使用 `allowed`、`waiting`、`running`、`succeeded` 等真实词汇。编号只用于真实过程，
不把普通卖点强行编号。Logo 以三条错位轨迹组成抽象爪痕，不使用吉祥物。

### 6.4 动效

- 首次进入时 Trace 在 5–7 秒内执行一轮。
- 章节进入视口时只做一次轻微位移与透明度变化。
- 安装命令复制后显示明确成功反馈。
- 不使用 WebGL、滚动劫持、持续视差、自定义鼠标或全页粒子。
- `prefers-reduced-motion: reduce` 下直接展示最终状态。

## 7. 技术架构

- 代码目录：仓库根目录 `website/`。
- 框架：Astro 7.2.0，默认静态输出。
- 文档：Starlight 0.41.7。
- 语言：TypeScript 6.0.3、Astro、原生 CSS；不引入 React、Tailwind 或通用组件库。
- 内容：`site-content.json` 保存双语首页文案，`project-facts.json` 保存共享事实和命令。
- 页面：`index.astro` 与 `en/index.astro` 复用同一个 `HomePage.astro`。
- 文档：Starlight `root` locale 为 `zh-CN`，`en` locale 为英文。
- 部署：Vercel 项目 Root Directory 为 `website`，构建命令 `npm run build`，输出 `dist`。

Astro 静态站不需要 Vercel adapter，也不需要环境变量、数据库或 Serverless Function。

## 8. 可访问性与响应式

- 文本与交互颜色至少满足 WCAG AA。
- 所有链接和按钮有可见键盘焦点。
- 复制命令按钮使用可读标签和 `aria-live` 状态。
- 桌面采用 12 列网格；移动端 Trace 改为垂直序列。
- 内容宽度、截图比例和标题换行在 360px、768px、1280px 三档验证。
- 图片包含描述性 alt，装饰图形从可访问性树隐藏。

## 9. 测试与验证

1. Node 内置测试先验证双语内容、共享事实、路由和构建产物，再写实现。
2. `astro check` 验证类型和内容 Schema。
3. `astro build` 验证所有静态路由。
4. 构建产物测试验证中文/英文首页、文档、hreflang、Claw Trace 和安装命令。
5. 本地 dev server 使用浏览器检查页面内容、交互、控制台错误与响应式截图。
6. Vercel Preview 部署后重复首页与文档检查。

仓库现有 Python 基线在官网分支创建前有 6 个与 32→33、101→102 数量更新相关的失败。
它们记录为既有问题，不属于官网变更，也不会在本任务中修改。

## 10. 明确不做

- 不新增登录、在线 Workspace、用户数据、后端 API 或数据库。
- 不展示动态 GitHub Star，避免运行时依赖和缓存问题。
- 不宣称真实平台 Live Gate 已完成。
- 不复制全部工程文档到 Starlight。
- 不生成虚假的客户 Logo、评价或产品控制台。
- 不在本任务中修改 Python Core、TUI 或既有 Eval 数量断言。
