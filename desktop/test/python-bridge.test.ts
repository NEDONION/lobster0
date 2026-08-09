import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { BridgeClient } from "@miniclaw/pi-tui/bridge-client";
import { expect, it } from "vitest";

const projectRoot = fileURLToPath(new URL("../../", import.meta.url));
const localPython = process.platform === "win32"
  ? join(projectRoot, ".venv", "Scripts", "python.exe")
  : join(projectRoot, ".venv", "bin", "python");
const python = process.env.MINICLAW_PYTHON
  ? resolve(projectRoot, process.env.MINICLAW_PYTHON)
  : (existsSync(localPython) ? localPython : "");

it.skipIf(!python)("handshakes with the real Python Bridge as Desktop", async () => {
  const temporaryRoot = mkdtempSync(join(tmpdir(), "miniclaw-desktop-"));
  const home = join(temporaryRoot, "home");
  const workspace = join(temporaryRoot, "workspace");
  mkdirSync(workspace);
  const environment = {
    ...process.env,
    MINICLAW_HOME: home,
    MINICLAW_PYTHON: python,
    MINICLAW_MODEL_API_KEY: "offline-smoke-key",
    MINICLAW_WORKSPACE: workspace,
    PYTHONPATH: join(projectRoot, "src"),
  };
  const initialized = spawnSync(python, ["-m", "miniclaw", "init", "--home", home], {
    cwd: projectRoot,
    encoding: "utf8",
    env: environment,
    shell: false,
    timeout: 5_000,
  });
  expect(initialized.status, initialized.stdout + initialized.stderr).toBe(0);

  const client = BridgeClient.spawnFromEnvironment(environment);
  try {
    const hello = await client.hello("miniclaw-desktop", "0.1.0");
    expect(hello.protocol).toBe(1);
    expect(hello.workspace).toBe(basename(workspace));
    expect(Array.isArray(hello.capabilities)).toBe(true);
    expect(hello.capabilities).toContain("automation_read");
    await client.shutdown();
  } finally {
    client.kill();
    rmSync(temporaryRoot, { force: true, recursive: true });
  }
}, 10_000);
