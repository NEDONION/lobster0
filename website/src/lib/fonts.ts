import localFont from 'next/font/local';

/**
 * Self-host the two web faces through `next/font` rather than Fontsource's
 * stylesheets. Fontsource ships plain CSS, so the browser cannot discover the
 * font files until that CSS has been fetched and parsed — an extra round trip
 * before any styled text can paint. `next/font` inlines the @font-face and
 * emits a matching `<link rel="preload">`, so the fonts start downloading with
 * the CSS instead of after it.
 *
 * Only the Latin subsets are declared: CJK is served by the system fonts in the
 * fallback stack, and the copy contains no latin-ext characters (French Œ/œ sit
 * inside the Latin range). Italic is left out on purpose — it is worth 31 KB
 * only if it is preloaded, and the handful of emphasised labels in the docs
 * read fine with the synthesised oblique.
 */
export const instrumentSans = localFont({
  display: 'swap',
  fallback: ['PingFang SC', 'Microsoft YaHei', 'system-ui', 'sans-serif'],
  src: [
    {
      path: '../../node_modules/@fontsource-variable/instrument-sans/files/instrument-sans-latin-wght-normal.woff2',
      style: 'normal',
      weight: '400 700',
    },
  ],
  variable: '--font-sans-web',
});

export const plexMono = localFont({
  display: 'swap',
  fallback: ['SFMono-Regular', 'Consolas', 'monospace'],
  src: [
    {
      path: '../../node_modules/@fontsource/ibm-plex-mono/files/ibm-plex-mono-latin-400-normal.woff2',
      style: 'normal',
      weight: '400',
    },
  ],
  variable: '--font-mono-web',
});
