# MiniClaw Next.js + Fumadocs 官网重构设计

## 1. 背景与目标

当前 `website/` 是 Astro + Starlight 实现，已经具备双语首页、双语用户文档、真实 TUI
截图和 Vercel 部署，但营销首页以连续纵向 Section 为主。实际浏览时页面偏长、留白偏多，
信息分散，无法在较短路径内体现 MiniClaw 的运行时、安全边界和真实工程证据。

本轮在原仓库内重构官网，不创建第二个网站仓库。最终代码仍位于仓库根目录的
`website/`，并继续作为独立 Vercel Project 的 Root Directory。

目标如下：

- 使用 Next.js 16 App Router 作为唯一 Web 应用框架。
- 在同一应用中使用 Fumadocs 提供专门的文档体验。
- 营销官网只保留一个约三屏长度的高密度首页；文档长度不计入三屏限制。
- 中文为默认语言，同时保留完整英文首页与英文文档。
- 通过有意义的动画和多组 Tabs 压缩内容，不通过删减事实换取页面变短。
- 继续只展示仓库中已实现、可验证的能力，明确区分 Implementation PASS 与 Live PASS。
- 先部署并验收 Vercel Preview，经过明确授权后再替换 Production。

## 2. 已选方案与排除方案

### 2.1 选择 Next.js + Fumadocs

Next.js 负责营销首页、路由、Metadata、图片、Server Components 和 Vercel 部署；Fumadocs
负责 MDX 内容源、文档页面树、侧栏、TOC、搜索、Tabs 和语言体验。两者位于一个
`website/package.json` 和一次部署中。

选择该组合的原因：

- 营销首页可以使用完整的 Next.js/React 组件体系，不必围绕文档主题做深度改造。
- Fumadocs 提供专门文档能力，同时允许共享品牌字体、颜色、链接和项目事实。
- Vercel 原生支持 Next.js，Preview 与 Production 的发布边界清晰。
- 当前文档不需要 Docusaurus 的多版本文档体系；引入版本化会增加内容复制与维护成本。

### 2.2 不采用 Docusaurus

Docusaurus 适合文档本身是主要产品、需要长期维护多个历史版本的大型项目。MiniClaw 当前
更需要高度定制的营销交互，同时只维护一套当前用户文档。为 Docusaurus 深度定制营销首页
和主题会增加 swizzle 与升级维护成本，因此本轮不采用。

### 2.3 不保留 Astro 与 Next.js 双站

不会让 Astro 继续承载营销页、另开 Next.js/Fumadocs 文档站。双应用会导致导航、SEO、
样式、依赖、Preview 和 Production 同步复杂化。Astro/Starlight 实现会在同一分支中被
Next.js/Fumadocs 原地替换，Git 历史保留旧实现。

## 3. 路由与语言

公开路由固定为：

- `/`：中文营销首页。
- `/en`：英文营销首页。
- `/docs` 与 `/docs/*`：中文文档。
- `/en/docs` 与 `/en/docs/*`：英文文档。
- `/sitemap.xml`、`/robots.txt`、Open Graph 与图标：Next.js Metadata 文件约定生成。

营销首页只存在上述两个本地化版本，不新增 `/product`、`/features` 或 `/use-cases` 等营销
路由。中文使用无前缀默认地址，英文使用 `/en` 前缀，避免改变已经公开的 URL 语义。

Fumadocs 使用本地 MDX 集合。中文是默认内容文件，英文使用 `.en.mdx` 后缀；中英文页面
必须成对存在。语言切换始终跳转到当前页面的对应语言，而不是回到文档首页。

## 4. 三屏信息架构

### 4.1 第一屏：Hero Runtime

桌面端目标高度约 `100vh`，包含：

- 精简主导航：MiniClaw、Product 锚点、Workbench 锚点、Docs、English/中文、GitHub。
- 一句话定位：“小而完整，真正能行动。”及一段不超过三行的说明。
- 主 CTA 指向安装文档，次 CTA 指向 GitHub。
- 可复制的源码安装命令。
- 动态 Claw Trace：`MESSAGE → AGENT → POLICY → APPROVAL → TOOL → RESULT`。
- 紧凑证据条：一个 Python Core、四个入口、18 Tools、33 Channel cases、15 Automation
  cases，并紧邻 Implementation PASS 边界说明。

Claw Trace 是首页唯一的主视觉签名。它表现真实运行时顺序，不伪装成可操作的生产后台。

### 4.2 第二屏：Capability Explorer

桌面端目标高度约 `110vh`。页面只渲染一个大型交互面板，通过以下 Tabs 原地切换内容：

- Runtime：消息进入 AgentRuntime、Policy、Approval、Tool、Result 的顺序。
- Channels：TUI、飞书、Telegram、Discord 共享 Core、隔离边缘故障域。
- Safety：Workspace、exact argv、SSRF、Secret 与四档 Permission Mode。
- Memory：Markdown Truth、SQLite Projection 与 Owner 边界。
- Automation：默认关闭、授权边界、versioned cases 与真实状态披露。

每个 Tab 固定包含三个层级：一句结论、结构图或状态面板、三至五条工程事实。内容区尺寸保持
稳定，切换时不造成页面跳动。Tab 使用 URL hash（如 `#safety`）支持刷新恢复和分享。

### 4.3 第三屏：Real Workbench

桌面端目标高度约 `90vh`，通过 Tabs 展示：

- SAFE Approval：真实 TUI 审批截图、风险说明、exact arguments 与结果。
- External CLI：真实 TUI 外部 CLI 截图、argv 结构和返回同一会话的结果。
- Multi-channel：同一 AgentRuntime 与隔离 Transport/Delivery/queue 的工作方式，不绘制
  不存在的平台后台截图。

工作台下方直接收束为 Quick Start、Docs、GitHub 和贡献入口，不再增加新的营销 Section。
桌面端完整首页目标为约 `2.8–3.2` 个视口高度。移动端允许自然增长，以可读性和 44px
触控目标优先，不强行压缩到三个移动视口。

## 5. 视觉系统

LobsterAI 只作为设计语言参考，不进行页面复刻。借鉴范围限定为三个原则：首屏用单一中心命题建立
认知、在首屏折线附近尽早展示产品本体、用任务与证据卡解释场景。MiniClaw 使用独立的浅色 Runtime
工作台、三屏高密度构图、技术字体、真实深色 TUI 证据与六步 Trace 标志性动效。

视觉方向命名为 **Interactive Runtime Instrument**：像一件紧凑、可信的工程仪器，而不是
松散的营销模板或虚构控制台。

### 5.1 颜色

- Canvas：`#F4F6FA`，全站冷白主背景。
- Surface：`#FFFFFF`，交互面板与浮层。
- Ink：`#10131A`，主文字和结构线重点。
- Terminal：`#171B24`，只用于真实 TUI、命令与局部运行状态，不铺满页面。
- Muted：`#667085`，辅助说明。
- Electric Blue：`#5B6CFF`，主动作与活动 Trace。
- Signal Mint：`#73F7C4`，允许、完成和健康状态。
- Approval Amber：`#F2B84B`，审批和风险提示。

不使用大面积渐变光球、装饰性网格或松散 Bento 卡片。结构由 12 列网格、克制阴影、细分隔线、状态标签、
日志行和真实的顺序关系建立。

### 5.2 字体

- Display/Body：Instrument Sans。
- Technical/Data：IBM Plex Mono。
- 中文：优先系统无衬线字体，保持清晰与构建稳定，不额外加载体积庞大的全量中文字库。

标题通过字宽、行高和对齐产生个性，不依赖超大字号制造空白。

## 6. 动画与交互

动画使用 Motion for React；简单 hover、focus 和 reduced-motion 状态使用 CSS。只有必须响应
用户或表现运行时顺序的组件是 Client Components。

### 6.1 Hero Trace

首次进入时用约 2.4 秒依次推进六个运行时状态。活动连线、事件标签、状态和日志同步变化，
完成后停在稳定最终态。不会无限循环整段动画。

### 6.2 Capability Tabs

切换 Tab 时，标题、事实、结构图和状态日志作为一个编排动作更新。使用短距离位移、裁剪揭示、
SVG path 推进和状态颜色过渡；避免只做整块淡入淡出。切换总时长控制在 180–320ms。

### 6.3 Workbench Tabs

进入工作流时，终端内容滚动、光标移动、审批区域高亮、结果出现。真实 TUI 截图保持原始事实，
动画只解释截图与执行顺序，不生成虚构产品界面。

### 6.4 可访问性与失败回退

- Tabs 使用标准 `tablist`、`tab`、`tabpanel` 语义并支持左右方向键、Home、End 和焦点环。
- URL hash 无效时回退到第一个 Tab，不产生错误页面。
- JavaScript 或 Motion 加载失败时，默认 Tab 和全部关键文案仍存在于服务端输出。
- `prefers-reduced-motion: reduce` 下不播放序列和位移动画，直接展示最终状态。
- 页面不可见时暂停非必要动画，避免后台持续消耗资源。

## 7. 组件与数据边界

首页组件按责任拆分：

- `MarketingHeader`：导航、语言与 GitHub，不管理页面内容状态。
- `HeroRuntime`：Hero 布局与安装入口。
- `ClawTrace`：六状态可视化与 reduced-motion 回退。
- `EvidenceStrip`：只消费共享项目事实。
- `CapabilityExplorer`：Tab 选择、hash 同步和面板容器。
- 每个 Capability Panel：只渲染所属能力的数据与图形。
- `Workbench`：工作流 Tabs 和真实截图说明。
- `CommandCopy`：复制命令与无 Clipboard API 回退。
- `MarketingFooter`：Docs、GitHub、Issues 与一句品牌陈述。

共享数据分为两类：

- `project-facts`：版本要求、工具数、入口数、评测 case 数、安装命令、状态边界和公开链接。
- `marketing-copy`：中文与英文的标题、说明、Tab 标签、CTA 和替代文本。

组件不得自行复制事实数字或安装命令。内容文件经过 TypeScript 类型检查，中英文结构不一致时
测试失败。

## 8. Fumadocs 文档设计

文档使用标准 Docs Layout：左侧页面树、顶部搜索、右侧 TOC、语言切换、前后页导航和移动端
菜单。视觉复用品牌颜色与字体，但不加载营销页的 Trace 和 Workbench 动画。

首批双语文档固定为：

1. Start / 从这里开始。
2. Install / 安装与启动。
3. Runtime / 运行时。
4. Security / 安全边界。
5. Channels / 多入口。
6. Memory / 记忆。

安装文档使用 Fumadocs Tabs 展示环境或平台差异。搜索采用内置 Orama，并针对中英文内容建立
索引。Fumadocs 运行在 Vercel Node Runtime；营销首页和文档内容仍在构建期预渲染，搜索接口
由同一 Next.js 应用提供。

## 9. SEO、性能与安全

- 使用 Next.js Metadata API 生成 title、description、canonical、hreflang、Open Graph、
  Twitter card、robots 和 sitemap。
- 为首页提供与品牌一致的 1200×630 Open Graph 图片，不使用通用渐变模板。
- 真实 TUI 图片使用 `next/image`、明确尺寸、WebP 和响应式 `sizes`。
- 营销动画按组件边界懒加载；Fumadocs 路由不加载营销动画 bundle。
- 不引入 CMS、数据库、分析脚本、身份系统或外部运行时 API。
- 所有外链使用明确可见的目标；Secret、Token 和个人数据不进入网站源码或部署配置。

## 10. 错误处理

- 自定义 `not-found` 页面提供首页、文档和 GitHub 返回路径。
- 缺少本地化营销文案、共享事实或对应 MDX 时在测试或构建阶段失败，不静默回退为混合语言。
- 复制命令失败时保留可选择的命令文本并显示可访问状态提示。
- Fumadocs 搜索不可用时文档页面树、TOC 和正常浏览不受影响。
- 图片加载失败不会隐藏标题和解释文字。

## 11. 测试与验收

### 11.1 自动检查

- TypeScript 类型检查、ESLint 和 Next.js production build 全部通过。
- 内容契约测试检查中英文结构、共享数字、安装命令和 Live Gate 文案。
- 组件测试覆盖 Tab 键盘操作、hash 初始化与更新、无效 hash、复制命令和 reduced-motion。
- Playwright 覆盖中文与英文首页、五个 Capability Tabs、三个 Workbench Tabs、语言切换、
  文档搜索、移动端菜单和全部内部链接。
- sitemap、robots、canonical、hreflang、OG image 和 404 具有直接断言。

### 11.2 浏览器验收

- 桌面端首页保持约三屏信息架构，无大片无信息留白。
- 390px 移动端无横向溢出，Tabs 可操作，触控目标不小于 44px。
- 正常模式和 reduced-motion 模式均可完整理解页面。
- 浏览器控制台无 error，全部 CSS、JS、图片和文档路由返回预期状态。
- 所有内部链接和锚点有效。

### 11.3 发布

- 在 `codex/miniclaw-website` 分支完成重构和验证。
- 从 `website/` 创建 Vercel Preview，检查构建日志与公开页面。
- 未经明确 Production 确认，不覆盖 `https://miniclaw.vercel.app`。
- Production 更新后重新检查 `/`、`/en`、`/docs`、`/en/docs`、sitemap、robots 和主要资源。

## 12. 迁移范围与非目标

迁移包括：现有双语文案、六组文档、真实 TUI 图片、项目事实、安装命令、品牌标记、SEO 与
Vercel 配置。旧 Astro/Starlight 构建文件和依赖在 Next.js 版本验证通过后删除。

本轮不包括：博客、价格页、登录、在线 Demo、云端 Agent、遥测后台、CMS、多版本文档、
用户评论、实时 GitHub Star 请求或任何尚未实现的产品能力。

## 13. 完成定义

只有同时满足以下条件才视为完成：

1. 官网源码全部位于仓库的 `website/`。
2. Next.js + Fumadocs 替换 Astro + Starlight，且不存在两套并行站点。
3. 中英文营销首页符合三屏高密度结构。
4. 五个 Capability Tabs、三个 Workbench Tabs 与三组动画按本设计工作。
5. 双语 Fumadocs 文档、搜索、导航和语言切换可用。
6. 自动检查、浏览器验收、Preview 构建与路由检查全部通过。
7. 经用户明确确认后，最新验证版本部署到 `https://miniclaw.vercel.app` 并再次通过验收。

## 14. 参考

- Next.js App Router：https://nextjs.org/docs/app
- Next.js Metadata：https://nextjs.org/docs/app/getting-started/metadata-and-og-images
- Fumadocs Next.js：https://www.fumadocs.dev/docs/manual-installation/next
- Fumadocs i18n：https://www.fumadocs.dev/docs/internationalization/next
- Fumadocs Page Tree：https://www.fumadocs.dev/docs/page-conventions
- Fumadocs Tabs：https://www.fumadocs.dev/docs/ui/components/tabs
