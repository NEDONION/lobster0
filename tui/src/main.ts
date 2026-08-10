/** Executable entry for the default Lobster0 pi-tui shell. */

const RELEASE_VERSION = "0.7.0";

function safeErrorCode(error: unknown): string {
  if (typeof error !== "object" || error === null || !("code" in error)) {
    return "tui_startup";
  }
  const code = error.code;
  return typeof code === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(code)
    ? code
    : "tui_startup";
}

async function smoke(): Promise<number> {
  const piTui = await import("@earendil-works/pi-tui");
  if (typeof piTui.ProcessTerminal !== "function" || typeof piTui.TuiAltScreen !== "function") {
    throw new Error("invalid pi-tui module");
  }
  process.stdout.write(
    `${JSON.stringify({ component: "pi-tui", version: RELEASE_VERSION, status: "ok" })}\n`,
  );
  return 0;
}

async function main(): Promise<number> {
  const arguments_ = process.argv.slice(2);
  if (arguments_.length === 1 && arguments_[0] === "--smoke") {
    return smoke();
  }
  if (!process.stdin.isTTY || !process.stdout.isTTY) {
    process.stderr.write("error: Lobster0 pi-tui requires an interactive terminal\n");
    return 2;
  }
  const [{ ProcessTerminal, TuiAltScreen }, { Lobster0Tui }, { BridgeClient }] =
    await Promise.all([
      import("@earendil-works/pi-tui"),
      import("./app.js"),
      import("./bridge-client.js"),
    ]);
  const terminal = new ProcessTerminal();
  const tui = new TuiAltScreen(terminal, false, undefined, { mouse: true });
  const bridge = BridgeClient.spawnFromEnvironment();
  const app = new Lobster0Tui({ tui, bridge, language: "zh-CN", sessionKey: "default" });
  const shutdown = () => app.stop(130);
  process.once("SIGINT", shutdown);
  process.once("SIGTERM", shutdown);
  try {
    await app.start();
    return app.waitForExit();
  } catch (error) {
    app.stop(2);
    throw error;
  }
}

try {
  process.exitCode = await main();
} catch (error) {
  const code = safeErrorCode(error);
  process.stderr.write(`error: Lobster0 pi-tui failed (${code})\n`);
  process.exitCode = 2;
}
