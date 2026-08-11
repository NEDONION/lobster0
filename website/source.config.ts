import { defineConfig, defineDocs } from 'fumadocs-mdx/config';

export const docs = defineDocs({
  dir: 'content/docs',
});

export default defineConfig({
  mdxOptions: {
    // The site is light-themed, but code blocks are deliberately dark so a
    // command looks identical in the hero and in the docs.
    rehypeCodeOptions: {
      themes: { dark: 'github-dark-default', light: 'github-dark-default' },
    },
  },
});
