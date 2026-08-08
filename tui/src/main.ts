/** Executable entry for the default MiniClaw pi-tui shell. */

import { ProcessTerminal, TuiAltScreen } from "@earendil-works/pi-tui";

import { MiniClawTui } from "./app.js";
import { BridgeClient, BridgeRequestError } from "./bridge-client.js";

async function main(): Promise<number> {
  if (!process.stdin.isTTY || !process.stdout.isTTY) {
    process.stderr.write("error: MiniClaw pi-tui requires an interactive terminal\n");
    return 2;
  }
  const terminal = new ProcessTerminal();
  const tui = new TuiAltScreen(terminal, false, undefined, { mouse: true });
  const bridge = BridgeClient.spawnFromEnvironment();
  const app = new MiniClawTui({ tui, bridge, language: "zh-CN", sessionKey: "default" });
  const shutdown = () => app.stop(130);
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
  await app.start();
  return app.waitForExit();
}

try {
  process.exitCode = await main();
} catch (error) {
  const code = error instanceof BridgeRequestError ? error.code : "tui_startup";
  process.stderr.write(`error: MiniClaw pi-tui failed (${code})\n`);
  process.exitCode = 2;
}
