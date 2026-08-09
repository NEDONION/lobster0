# MiniClaw Website

MiniClaw 的双语营销官网与用户文档。网站是独立的静态 Astro 项目，源码全部位于仓库根目录的
`website/`，不会把 Node 依赖引入 Python Core。

## Local development

```bash
cd website
npm install
npm run dev
```

交付前运行完整网站门禁：

```bash
npm run check
npm run build
npm test
```

## Content and routes

- 中文首页：`/`
- 英文首页：`/en/`
- 中文文档：`/docs/`
- 英文文档：`/en/docs/`
- 双语首页内容：`src/content-data/site-content.json`
- 共享数字、链接与安装命令：`src/content-data/project-facts.json`

## Vercel

- Root Directory: `website`
- Framework Preset: `Astro`
- Build Command: `npm run build`
- Output Directory: `dist`
- Install Command: `npm ci`

Astro 使用默认静态输出，不需要 Vercel adapter、Serverless Function、数据库或环境变量。绑定正式
域名后，可以设置 `PUBLIC_SITE_URL` 生成 canonical URL；Preview 未设置时不会写入错误的 canonical。
