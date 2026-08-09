import { createMDX } from 'fumadocs-mdx/next';

const withMDX = createMDX();

export default withMDX({
  reactStrictMode: true,
  images: {
    formats: ['image/avif', 'image/webp'],
  },
});
