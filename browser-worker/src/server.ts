import { Buffer } from "node:buffer";

import {
  MAX_FRAME_BYTES,
  PROTOCOL,
  ProtocolError,
  encodeResponse,
  parseRequest,
} from "./protocol.js";

let pending = Buffer.alloc(0);
let stopped = false;

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
    try {
      const request = parseRequest(frame);
      process.stdout.write(
        encodeResponse({
          protocol: PROTOCOL,
          id: request.id,
          ok: false,
          error: {
            code: "browser_action_unavailable",
            message: "Browser action is not available",
          },
        }),
      );
    } catch (error) {
      fail(error instanceof ProtocolError ? error.code : "invalid_frame");
    }
  }
});
process.stdin.on("end", () => {
  if (!stopped && pending.byteLength > 0) fail("incomplete_frame");
});

function fail(code: string): void {
  stopped = true;
  process.stderr.write(`browser_protocol_error:${code}\n`);
  process.exitCode = 2;
  process.stdin.pause();
}
