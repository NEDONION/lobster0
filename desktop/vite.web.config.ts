import react from "@vitejs/plugin-react";
import { rename } from "node:fs/promises";
import { resolve } from "node:path";
import { defineConfig } from "vite";

const OUT_DIR = "out/web/client";

/**
 * Web 控制台的浏览器产物。
 *
 * Renderer 源码原样复用，只把入口换成 `src/web/main.tsx`——那里装的是
 * fetch + SSE 版的 `window.lobster0`，而不是 Electron preload。
 */
export default defineConfig({
  root: ".",
  plugins: [
    react(),
    {
      // Vite 用源文件名命名 HTML 产物，于是会得到 `web.html`；server 的默认文档
      // 是 `index.html`，这里在写盘后改名，避免为此在 server 里引入特例。
      name: "lobster0-console-index",
      async writeBundle() {
        const directory = resolve(import.meta.dirname, OUT_DIR);
        await rename(resolve(directory, "web.html"), resolve(directory, "index.html"));
      },
    },
  ],
  build: {
    outDir: OUT_DIR,
    emptyOutDir: true,
    rollupOptions: {
      input: resolve(import.meta.dirname, "web.html"),
    },
  },
});
