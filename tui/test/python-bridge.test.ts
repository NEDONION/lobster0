import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { BridgeClient } from "../dist/bridge-client.js";

const projectRoot = fileURLToPath(new URL("../../", import.meta.url));
const configuredPython = process.env.LOBSTER0_PYTHON;
const localPython = process.platform === "win32"
  ? join(projectRoot, ".venv", "Scripts", "python.exe")
  : join(projectRoot, ".venv", "bin", "python");
const python = configuredPython || (existsSync(localPython) ? localPython : "");

test(
  "real TypeScript client handshakes with the Python Core bridge",
  { skip: python ? false : "LOBSTER0_PYTHON or the project .venv is required" },
  async () => {
    assert.ok(python);
    const configuredHome = process.env.LOBSTER0_BRIDGE_SMOKE_HOME;
    const smokeHome = configuredHome || mkdtempSync(join(tmpdir(), "lobster0-pi-tui-"));
    const environment = {
      ...process.env,
      LOBSTER0_HOME: smokeHome,
      LOBSTER0_PYTHON: python,
      LOBSTER0_MODEL_API_KEY: "offline-smoke-key",
      PYTHONPATH: join(projectRoot, "src"),
    };
    if (!configuredHome) {
      const initialized = spawnSync(python, ["-m", "lobster0", "init", "--home", smokeHome], {
        cwd: projectRoot,
        env: environment,
        encoding: "utf8",
        shell: false,
        timeout: 5_000,
      });
      assert.equal(initialized.status, 0, initialized.stdout + initialized.stderr);
    }
    const client = BridgeClient.spawnFromEnvironment({
      ...environment,
    });
    try {
      const hello = await client.hello();
      assert.equal(hello.protocol, 1);
      assert.equal(hello.core_version, "0.7.0");
      assert.equal(Array.isArray(hello.tools), true);
      assert.equal(Array.isArray(hello.capabilities), true);
      await client.shutdown();
    } finally {
      client.kill();
      if (!configuredHome) {
        rmSync(smokeHome, { force: true, recursive: true });
      }
    }
  },
);
