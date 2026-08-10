import { app, BrowserWindow, dialog, ipcMain, shell } from "electron";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { DESKTOP_CHANNELS } from "../common/api";
import { BridgeService } from "./bridge-service";
import { registerDesktopIpc } from "./ipc";

const currentDirectory = dirname(fileURLToPath(import.meta.url));
const bridge = new BridgeService();
let quitting = false;

function createWindow(): void {
  const window = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 900,
    minHeight: 640,
    backgroundColor: "#f8f9fb",
    titleBarStyle: "hiddenInset",
    webPreferences: {
      preload: join(currentDirectory, "../preload/index.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  window.webContents.setWindowOpenHandler(({ url }) => {
    if (isExternalHttpUrl(url)) {
      void shell.openExternal(url);
    }
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    if (url === window.webContents.getURL()) {
      return;
    }
    event.preventDefault();
    if (isExternalHttpUrl(url)) {
      void shell.openExternal(url);
    }
  });

  const rendererUrl = process.env.ELECTRON_RENDERER_URL;
  if (!app.isPackaged && rendererUrl) {
    void window.loadURL(rendererUrl);
    return;
  }
  void window.loadFile(join(currentDirectory, "../renderer/index.html"));
}

function isExternalHttpUrl(url: string): boolean {
  try {
    const protocol = new URL(url).protocol;
    return protocol === "https:" || protocol === "http:";
  } catch {
    return false;
  }
}

void app.whenReady().then(() => {
  registerDesktopIpc(
    (channel, handler) => {
      ipcMain.handle(channel, (_event, payload) => handler(payload));
    },
    bridge,
    (frame) => {
      for (const window of BrowserWindow.getAllWindows()) {
        window.webContents.send(DESKTOP_CHANNELS.frame, frame);
      }
    },
    async () => {
      const result = await dialog.showOpenDialog({ properties: ["openDirectory"] });
      return result.canceled ? null : (result.filePaths[0] ?? null);
    },
  );
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("before-quit", (event) => {
  if (quitting || bridge.status === "stopped") {
    return;
  }
  event.preventDefault();
  void bridge.stop().finally(() => {
    quitting = true;
    app.quit();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
