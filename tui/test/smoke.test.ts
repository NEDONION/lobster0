/** Checkout-independent pi-tui smoke contract. */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import test from "node:test";

test("smoke imports real pi-tui before TTY or Bridge startup", () => {
  const result = spawnSync(process.execPath, ["dist/main.js", "--smoke"], {
    cwd: process.cwd(),
    encoding: "utf8",
    env: {
      MINICLAW_HOME: "/does/not/exist",
      MINICLAW_PYTHON: "/does/not/exist/python",
      PATH: "",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });

  assert.equal(result.status, 0);
  assert.equal(
    result.stdout,
    '{"component":"pi-tui","version":"0.7.0","status":"ok"}\n',
  );
  assert.equal(result.stderr, "");
});
