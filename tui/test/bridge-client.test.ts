import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import test from "node:test";

import {
  BridgeClient,
  BridgeRequestError,
  buildBridgeSpawnSpec,
  type BridgeProcess,
} from "../dist/bridge-client.js";

class FakeProcess extends EventEmitter implements BridgeProcess {
  public readonly stdin = new PassThrough();
  public readonly stdout = new PassThrough();
  public readonly stderr = new PassThrough();
  public killed = false;

  public kill(): boolean {
    this.killed = true;
    return true;
  }
}

test("Desktop workspace is passed as an explicit Bridge argument", () => {
  const spec = buildBridgeSpawnSpec({
    MINICLAW_PYTHON: "/opt/miniclaw/python",
    MINICLAW_HOME: "/state/miniclaw",
    MINICLAW_WORKSPACE: "/work/report",
  });

  assert.equal(spec.program, "/opt/miniclaw/python");
  assert.deepEqual(spec.args, [
    "-m",
    "miniclaw.bridge",
    "--home",
    "/state/miniclaw",
    "--workspace",
    "/work/report",
  ]);
});

test("relative Desktop workspace is rejected before spawning Python", () => {
  assert.throws(
    () =>
      buildBridgeSpawnSpec({
        MINICLAW_PYTHON: "/opt/miniclaw/python",
        MINICLAW_HOME: "/state/miniclaw",
        MINICLAW_WORKSPACE: "relative/report",
      }),
    (error: unknown) =>
      error instanceof BridgeRequestError && error.code === "bridge_configuration",
  );
});

test("hello sends the Desktop client identity when provided", async () => {
  const process = new FakeProcess();
  const client = new BridgeClient(process);
  const written: Buffer[] = [];
  process.stdin.on("data", (chunk: Buffer) => written.push(chunk));

  const pending = client.hello("miniclaw-desktop", "0.1.0");
  await new Promise((resolve) => setImmediate(resolve));
  const request = JSON.parse(Buffer.concat(written).toString("utf8"));
  assert.deepEqual(request.payload, {
    client_name: "miniclaw-desktop",
    client_version: "0.1.0",
    protocols: [1],
  });
  process.stdout.write(
    `${JSON.stringify({ v: 1, id: request.id, type: "response.ok", payload: {} })}\n`,
  );
  await pending;
});

test("hello keeps the pi-tui identity by default", async () => {
  const process = new FakeProcess();
  const client = new BridgeClient(process);
  const written: Buffer[] = [];
  process.stdin.on("data", (chunk: Buffer) => written.push(chunk));

  const pending = client.hello();
  await new Promise((resolve) => setImmediate(resolve));
  const request = JSON.parse(Buffer.concat(written).toString("utf8"));
  assert.equal(request.payload.client_name, "miniclaw-pi-tui");
  process.stdout.write(
    `${JSON.stringify({ v: 1, id: request.id, type: "response.ok", payload: {} })}\n`,
  );
  await pending;
});

test("request writes NDJSON and resolves its matching response", async () => {
  const process = new FakeProcess();
  const client = new BridgeClient(process);
  const written: Buffer[] = [];
  process.stdin.on("data", (chunk: Buffer) => written.push(chunk));

  const response = client.request("client.hello", {
    client_name: "test",
    client_version: "0.1.0",
    protocols: [1],
  });
  await new Promise((resolve) => setImmediate(resolve));
  const request = JSON.parse(Buffer.concat(written).toString("utf8"));
  process.stdout.write(
    `${JSON.stringify({
      v: 1,
      id: request.id,
      type: "response.ok",
      payload: { model: "deepseek-v4-pro" },
    })}\n`,
  );

  assert.deepEqual(await response, { model: "deepseek-v4-pro" });
});

test("setPermissionMode sends the exact versioned request and returns the Core mode", async () => {
  const process = new FakeProcess();
  const client = new BridgeClient(process);
  const written: Buffer[] = [];
  process.stdin.on("data", (chunk: Buffer) => written.push(chunk));

  const pending = client.setPermissionMode("autopilot");
  await new Promise((resolve) => setImmediate(resolve));
  const request = JSON.parse(Buffer.concat(written).toString("utf8"));
  assert.equal(request.type, "permissions.set");
  assert.deepEqual(request.payload, { mode: "autopilot" });
  process.stdout.write(
    `${JSON.stringify({
      v: 1,
      id: request.id,
      type: "response.ok",
      payload: { permission_mode: "autopilot" },
    })}\n`,
  );

  assert.equal(await pending, "autopilot");
});

test("fragmented RunEvent frames reach subscribers in order", async () => {
  const process = new FakeProcess();
  const client = new BridgeClient(process);
  const received: string[] = [];
  client.onEvent((frame) => received.push(`${frame.type}:${String(frame.payload.text ?? "")}`));
  const bytes = Buffer.from(
    '{"v":1,"type":"event.model_reasoning","payload":{"turn_id":1,"text":"检查"}}\n' +
      '{"v":1,"type":"event.model_text_delta","payload":{"turn_id":1,"text":"完成"}}\n',
  );

  process.stdout.write(bytes.subarray(0, 17));
  process.stdout.write(bytes.subarray(17, 71));
  process.stdout.write(bytes.subarray(71));
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(received, ["event.model_reasoning:检查", "event.model_text_delta:完成"]);
});

test("safe response errors reject one request without closing the client", async () => {
  const process = new FakeProcess();
  const client = new BridgeClient(process);
  const written: Buffer[] = [];
  process.stdin.on("data", (chunk: Buffer) => written.push(chunk));

  const pending = client.request("turn.start", { session_key: "default", text: "hello" });
  await new Promise((resolve) => setImmediate(resolve));
  const request = JSON.parse(Buffer.concat(written).toString("utf8"));
  process.stdout.write(
    `${JSON.stringify({
      v: 1,
      id: request.id,
      type: "response.error",
      payload: { code: "turn_busy", message: "已有任务正在运行", retryable: true },
    })}\n`,
  );

  await assert.rejects(
    pending,
    (error: unknown) =>
      error instanceof BridgeRequestError && error.code === "turn_busy" && error.retryable,
  );
  const next = client.request("turn.cancel", {});
  await new Promise((resolve) => setImmediate(resolve));
  const lines = Buffer.concat(written).toString("utf8").trim().split("\n");
  const cancel = JSON.parse(lines.at(-1) ?? "{}");
  process.stdout.write(
    `${JSON.stringify({ v: 1, id: cancel.id, type: "response.ok", payload: {} })}\n`,
  );
  assert.deepEqual(await next, {});
});

test("process exit rejects pending work with bounded diagnostics", async () => {
  const process = new FakeProcess();
  const client = new BridgeClient(process);
  process.stderr.write("sensitive provider response that must not be copied");
  const pending = client.request("turn.start", { session_key: "default", text: "hello" });

  process.emit("exit", 5, null);

  await assert.rejects(
    pending,
    (error: unknown) =>
      error instanceof BridgeRequestError &&
      error.code === "bridge_exited" &&
      !error.message.includes("sensitive provider"),
  );
});

test("shutdown waits for the Python process to finish cleanup", async () => {
  const process = new FakeProcess();
  const client = new BridgeClient(process);
  const written: Buffer[] = [];
  process.stdin.on("data", (chunk: Buffer) => written.push(chunk));

  const shutdown = client.shutdown();
  await new Promise((resolve) => setImmediate(resolve));
  const request = JSON.parse(Buffer.concat(written).toString("utf8"));
  process.stdout.write(
    `${JSON.stringify({ v: 1, id: request.id, type: "response.ok", payload: {} })}\n`,
  );
  await new Promise((resolve) => setImmediate(resolve));
  let settled = false;
  void shutdown.then(() => {
    settled = true;
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(settled, false);

  process.emit("exit", 0, null);
  await shutdown;
  assert.equal(settled, true);
});
