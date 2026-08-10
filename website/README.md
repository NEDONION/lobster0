# Lobster0 Website

Lobster0 的双语官网和用户文档，使用 Next.js 16 App Router、React 19 与 Fumadocs 构建。
网站代码、内容、测试和部署配置全部位于仓库根目录的 `website/`，与 Python Core 保持依赖隔离。

## Code map

- `src/app/[lang]/`：中文和英文官网、Fumadocs 文档路由。
- `src/components/marketing/`：三屏官网、Claw Trace、Capability Explorer 和 Workbench。
- `content/docs/`：中英文 MDX 文档；英文文件使用 `.en.mdx` 后缀。
- `src/content/site.ts`：双语营销文案与经过核实的项目事实。
- `src/styles/globals.css`：官网与文档共享的浅色视觉系统。
- `tests/e2e/`：桌面与移动端 Playwright journeys、搜索和链接门禁。

## Local development

需要 Node.js 22.19 或更高版本。

```bash
cd website
npm ci
npm run dev
```

本地地址为 `http://localhost:3000`。交付前运行：

```bash
npm run typecheck
npm run lint
npm test
npm run build
npm run test:e2e
```

Playwright 在本地优先使用已安装的 Chrome；CI 需要先安装 Playwright Chromium。

## Routes

- 中文官网：`/`
- 英文官网：`/en`
- 中文文档：`/docs`
- 英文文档：`/en/docs`

搜索接口位于 `/api/search`；`sitemap.xml`、`robots.txt` 和 Open Graph 图片由 Next.js Metadata
文件约定生成。

## Vercel

Vercel 项目使用以下配置：

- Root Directory：`website`
- Framework Preset：`Next.js`
- Install Command：`npm ci`
- Build Command：`npm run build`
- Output Directory：留空，由 Next.js 管理 `.next`
- Production URL：`https://lobster0.vercel.app`

每次发布先创建 Vercel Preview，完成双语官网、文档、搜索、链接和移动端验证后，才能将同一个
候选提交部署到 Production。Production 部署必须得到明确授权，不从本地未验证文件树直接覆盖。
