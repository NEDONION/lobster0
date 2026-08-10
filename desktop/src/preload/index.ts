import { contextBridge, ipcRenderer } from "electron";

import { createDesktopApi } from "../common/api";

const api = createDesktopApi(
  (channel, payload) => ipcRenderer.invoke(channel, payload),
  (channel, handler) => {
    const listener = (_event: Electron.IpcRendererEvent, value: unknown): void => handler(value);
    ipcRenderer.on(channel, listener);
    return () => ipcRenderer.removeListener(channel, listener);
  },
);

contextBridge.exposeInMainWorld("lobster0", api);
