import { defineI18nUI } from 'fumadocs-ui/i18n';
import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';

import { marketingCopy, siteFacts, type Locale } from '@/content/site';
import { BrandMark } from '@/components/marketing/BrandMark';
import { i18n, localizedPath } from '@/lib/i18n';

export const i18nUI = defineI18nUI(i18n, {
  'zh-CN': {
    displayName: '简体中文',
    'Choose a language(language switcher)': '选择语言',
    'Choose a language(language switcher)(aria-label)': '选择语言',
    'Close Search(search dialog)(aria-label)': '关闭搜索',
    'Close Sidebar(sidebar)(aria-label)': '关闭侧边栏',
    'Collapse Sidebar(sidebar)(aria-label)': '收起侧边栏',
    'Hide Sidebar(sidebar)': '收起侧边栏',
    'Show Sidebar(sidebar)': '展开侧边栏',
    'Copied Text(code block)(aria-label)': '已复制',
    'Copy Text(code block)(aria-label)': '复制代码',
    'Next Page(pagination)': '下一页',
    'No Headings(table of contents)': '本页无目录',
    'No results found(search dialog)': '没有找到结果',
    'On this page(table of contents)': '本页内容',
    'Open Search(search trigger)(aria-label)': '打开搜索',
    'Open Sidebar(sidebar)(aria-label)': '打开侧边栏',
    'Previous Page(pagination)': '上一页',
    'Search(search dialog)': '搜索文档',
    'Search(search trigger)': '搜索',
    'Toggle Menu(mobile menu)(aria-label)': '切换菜单',
  },
  en: {
    displayName: 'English',
  },
});

export function baseOptions(locale: Locale): BaseLayoutProps {
  const copy = marketingCopy[locale];

  return {
    links: [
      {
        text: locale === 'zh-CN' ? '官网' : 'Website',
        url: localizedPath(locale, '/'),
      },
      {
        external: true,
        text: copy.nav.github,
        url: siteFacts.links.github,
      },
    ],
    nav: {
      title: (
        <span className="docs-brand">
          <BrandMark size={26} />
          Lobster0
        </span>
      ),
      url: localizedPath(locale, '/docs'),
    },
    themeSwitch: {
      enabled: false,
    },
  };
}
