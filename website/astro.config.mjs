import starlight from '@astrojs/starlight';
import { defineConfig } from 'astro/config';

export default defineConfig({
  integrations: [
    starlight({
      title: {
        'zh-CN': 'MiniClaw 文档',
        en: 'MiniClaw Docs',
      },
      description: 'MiniClaw：小而完整、可自托管、边界可控的个人 AI Agent。',
      locales: {
        root: {
          label: '简体中文',
          lang: 'zh-CN',
        },
        en: {
          label: 'English',
          lang: 'en',
        },
      },
      favicon: '/favicon.svg',
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/NEDONION/miniclaw',
        },
      ],
      sidebar: [
        {
          label: '用户指南',
          translations: { en: 'User guide' },
          items: [{ autogenerate: { directory: 'docs' } }],
        },
      ],
      customCss: ['./src/styles/docs.css'],
    }),
  ],
});
