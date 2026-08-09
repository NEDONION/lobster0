import react from "@vitejs/plugin-react";
import { defineConfig } from "electron-vite";

export default defineConfig({
  main: {},
  preload: {},
  renderer: {
    root: ".",
    plugins: [react()],
    build: {
      rollupOptions: {
        input: "index.html",
      },
    },
  },
});
