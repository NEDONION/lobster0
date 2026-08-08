import assert from "node:assert/strict";
import test from "node:test";

import { BridgeClient } from "../dist/bridge-client.js";

const smokeHome = process.env.MINICLAW_BRIDGE_SMOKE_HOME;

test(
  "real TypeScript client handshakes with the Python Core bridge",
  { skip: smokeHome ? false : "MINICLAW_BRIDGE_SMOKE_HOME is not configured" },
  async () => {
    assert.ok(smokeHome);
    const client = BridgeClient.spawnFromEnvironment({
      ...process.env,
      MINICLAW_HOME: smokeHome,
    });

    const hello = await client.hello();
    assert.equal(hello.protocol, 1);
    assert.equal(hello.core_version, "0.1.0");
    assert.equal(Array.isArray(hello.tools), true);
    assert.equal(Array.isArray(hello.capabilities), true);

    await client.shutdown();
  },
);
