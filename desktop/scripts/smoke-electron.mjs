// Desktop smoke: boots the real Electron main process (which spawns the real Python
// Bridge), waits for the renderer to settle, asserts the shell, captures the window,
// then quits. Run `pnpm build` first; see docs for the required MINICLAW_* env vars.
import { app, BrowserWindow } from "electron";
import { writeFile } from "node:fs/promises";

const output = process.env.SMOKE_OUTPUT ?? "smoke-desktop.png";

await import("../out/main/index.js");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function captureWhenReady() {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const [window] = BrowserWindow.getAllWindows();
    if (window && !window.webContents.isLoading()) {
      // Let bootstrap() + listSessions() resolve and paint.
      await sleep(3000);
      const composer = await window.webContents.executeJavaScript(
        `(() => {
          const textarea = document.querySelector('textarea[aria-label="消息内容"]');
          const create = [...document.querySelectorAll('button')]
            .find((b) => b.textContent.includes('新建对话'));
          const dragRegion = document.querySelector('.app-drag-region');
          const dragStyle = dragRegion ? getComputedStyle(dragRegion) : null;
          return JSON.stringify({
            composerVisible: Boolean(textarea),
            composerEnabled: Boolean(textarea && !textarea.disabled),
            placeholder: textarea ? textarea.placeholder : null,
            createTaskInSidebar: Boolean(create && create.closest('.sidebar')),
            navLabels: [...document.querySelectorAll('.nav-item')].map((n) => n.textContent.trim()),
            homeGridGone: !document.querySelector('.home-grid'),
            statusTrack: document.querySelector('.composer-actions > span')?.textContent ?? null,
            dragRegionAppRegion: dragStyle ? dragStyle.getPropertyValue('-webkit-app-region') : null,
          });
        })()`,
      );
      const image = await window.webContents.capturePage();
      await writeFile(output, image.toPNG());
      console.log("SMOKE_RESULT " + composer);
      console.log("SMOKE_SCREENSHOT " + output);
      return assert(JSON.parse(composer));
    }
    await sleep(500);
  }
  console.log("SMOKE_RESULT {\"error\":\"window never settled\"}");
  return 1;
}

// D1 shell contract: the first paint is the task composer, wired to a live Core.
function assert(state) {
  const failures = [];
  if (!state.composerVisible) failures.push("composer missing on first paint");
  if (!state.composerEnabled) failures.push("composer disabled — Core never became ready");
  if (!state.createTaskInSidebar) failures.push("新建对话 not in the shared sidebar");
  if (!state.homeGridGone) failures.push("legacy home grid still rendered");
  if (state.navLabels.length !== 3) failures.push("expected exactly 3 nav views");
  if (!state.statusTrack?.startsWith("Main Agent · ")) {
    failures.push("composer status track missing real Core values");
  }
  if (state.dragRegionAppRegion !== "drag") {
    failures.push("window drag region missing — window would be unmovable");
  }
  for (const failure of failures) {
    console.log("SMOKE_FAIL " + failure);
  }
  return failures.length === 0 ? 0 : 1;
}

void app.whenReady().then(async () => {
  let code = 1;
  try {
    code = await captureWhenReady();
  } catch (error) {
    console.log("SMOKE_RESULT {\"error\":" + JSON.stringify(String(error)) + "}");
  } finally {
    app.exit(code);
  }
});
