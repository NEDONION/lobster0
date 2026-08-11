import { defineConfig } from "vite";

/**
 * Web 控制台的 Node server 产物。
 *
 * 除 Node 内建模块外全部打进单文件，因此 launcher 可以直接
 * `node out/web/server/index.js`，不依赖 cwd 与 node_modules 的解析路径。
 * 前提是 `pnpm --dir tui build` 已经产出 `tui/dist`。
 */
export default defineConfig({
  build: {
    ssr: "src/web/server-main.ts",
    outDir: "out/web/server",
    emptyOutDir: true,
    target: "node22",
    minify: false,
    rollupOptions: {
      external: [/^node:/],
      output: {
        format: "es",
        entryFileNames: "index.js",
      },
    },
  },
});
