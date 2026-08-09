import { Buffer } from "node:buffer";

import { ActionExecutor, BrowserActionError } from "./actions.js";
import {
  MAX_FRAME_BYTES,
  PROTOCOL,
  ProtocolError,
  encodeResponse,
  parseRequest,
} from "./protocol.js";
import { SessionManager } from "./sessions.js";

const options = parseOptions(process.argv.slice(2));
const sessions = new SessionManager({
  profileRoot: options.profileRoot,
  executablePath: options.executablePath,
  maxTabs: options.maxTabs,
  inactivityTimeoutMs: options.inactivityTimeoutMs,
  headed: options.headed,
});
const actions = new ActionExecutor(sessions, {
  maxSnapshotChars: options.maxSnapshotChars,
  stagingRoot: options.stagingRoot,
  maxArtifactBytes: options.maxArtifactBytes,
});
let pending = Buffer.alloc(0);
let stopped = false;
let queued = 0;
let chain = Promise.resolve();

process.stdout.write(`${JSON.stringify({ protocol: PROTOCOL, type: "ready" })}\n`);
process.stdin.on("data", (chunk: Buffer) => {
  if (stopped) return;
  pending = Buffer.concat([pending, chunk]);
  if (pending.byteLength > MAX_FRAME_BYTES && pending.indexOf(0x0a) === -1) {
    fail("frame_too_large");
    return;
  }
  while (!stopped) {
    const newline = pending.indexOf(0x0a);
    if (newline === -1) return;
    const frame = pending.subarray(0, newline + 1);
    pending = pending.subarray(newline + 1);
    queued += 1;
    if (queued > 16) {
      fail("too_many_requests");
      return;
    }
    chain = chain
      .then(() => handleFrame(frame))
      .catch(() => fail("browser_action_failed"))
      .finally(() => {
        queued -= 1;
      });
  }
});
process.stdin.on("end", () => {
  chain = chain.then(async () => {
    if (!stopped && pending.byteLength > 0) {
      fail("incomplete_frame");
      return;
    }
    stopped = true;
    await sessions.close();
  });
});
for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.once(signal, () => {
    stopped = true;
    void sessions.close().finally(() => process.exit(0));
  });
}

async function handleFrame(frame: Buffer): Promise<void> {
  let request;
  try {
    request = parseRequest(frame);
  } catch (error) {
    fail(error instanceof ProtocolError ? error.code : "invalid_frame");
    return;
  }
  try {
    const result = await actions.execute(request);
    process.stdout.write(
      encodeResponse({ protocol: PROTOCOL, id: request.id, ok: true, result }),
    );
  } catch (error) {
    const code =
      error instanceof BrowserActionError ? error.code : "browser_action_failed";
    process.stdout.write(
      encodeResponse({
        protocol: PROTOCOL,
        id: request.id,
        ok: false,
        error: { code, message: "Browser action failed" },
      }),
    );
  }
}

function fail(code: string): void {
  if (stopped) return;
  stopped = true;
  process.stderr.write(`browser_protocol_error:${code}\n`);
  process.exitCode = 2;
  process.stdin.pause();
  void sessions.close();
}

interface ServerOptions {
  profileRoot: string;
  executablePath: string;
  maxTabs: number;
  inactivityTimeoutMs: number;
  headed: boolean;
  maxSnapshotChars: number;
  stagingRoot: string;
  maxArtifactBytes: number;
}

function parseOptions(argumentsList: string[]): ServerOptions {
  const values = new Map<string, string>();
  for (const argument of argumentsList) {
    const separator = argument.indexOf("=");
    if (!argument.startsWith("--") || separator < 3) invalidOptions();
    const name = argument.slice(2, separator);
    const value = argument.slice(separator + 1);
    if (!value || values.has(name)) invalidOptions();
    values.set(name, value);
  }
  const expected = new Set([
    "profile-root",
    "executable-path",
    "max-tabs",
    "inactivity-timeout-ms",
    "headed",
    "max-snapshot-chars",
    "staging-root",
    "max-artifact-bytes",
  ]);
  if (values.size !== expected.size || [...values.keys()].some((key) => !expected.has(key))) {
    invalidOptions();
  }
  const headed = values.get("headed");
  if (headed !== "true" && headed !== "false") invalidOptions();
  return {
    profileRoot: required(values, "profile-root"),
    executablePath: required(values, "executable-path"),
    maxTabs: positiveInteger(values, "max-tabs", 32),
    inactivityTimeoutMs: positiveInteger(values, "inactivity-timeout-ms", 86_400_000),
    headed: headed === "true",
    maxSnapshotChars: positiveInteger(values, "max-snapshot-chars", 100_000),
    stagingRoot: required(values, "staging-root"),
    maxArtifactBytes: positiveInteger(values, "max-artifact-bytes", 100 * 1024 * 1024),
  };
}

function required(values: Map<string, string>, name: string): string {
  const value = values.get(name);
  if (value === undefined) invalidOptions();
  return value;
}

function positiveInteger(values: Map<string, string>, name: string, maximum: number): number {
  const value = Number(required(values, name));
  if (!Number.isInteger(value) || value < 1 || value > maximum) invalidOptions();
  return value;
}

function invalidOptions(): never {
  process.stderr.write("browser_protocol_error:browser_options_invalid\n");
  process.exit(2);
}
