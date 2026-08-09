import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_FRAME_BYTES,
  PROTOCOL,
  ProtocolError,
  encodeResponse,
  parseRequest,
} from "../dist/protocol.js";

test("accepts one bounded versioned action envelope", () => {
  const request = parseRequest(
    `${JSON.stringify({
      protocol: PROTOCOL,
      id: "request-1",
      session_id: "session-1",
      action: "close",
      params: {},
    })}\n`,
  );

  assert.equal(request.action, "close");
  assert.equal(request.session_id, "session-1");
});

test("rejects oversized, unknown and extensible action payloads", () => {
  assert.throws(
    () => parseRequest(Buffer.alloc(MAX_FRAME_BYTES + 1, 0x20)),
    (error: unknown) => error instanceof ProtocolError && error.code === "frame_too_large",
  );
  assert.throws(
    () =>
      parseRequest(
        JSON.stringify({
          protocol: PROTOCOL,
          id: "request-1",
          session_id: "session-1",
          action: "eval",
          params: {},
        }),
      ),
    (error: unknown) => error instanceof ProtocolError && error.code === "unknown_action",
  );
  assert.throws(
    () =>
      parseRequest(
        JSON.stringify({
          protocol: PROTOCOL,
          id: "request-1",
          session_id: "session-1",
          action: "close",
          params: {},
          script: "alert(1)",
        }),
      ),
    (error: unknown) => error instanceof ProtocolError && error.code === "invalid_envelope",
  );
});

test("encodes one compact correlated response line", () => {
  const line = encodeResponse({
    protocol: PROTOCOL,
    id: "request-1",
    ok: true,
    result: { accepted: true },
  });

  assert.equal(line.endsWith("\n"), true);
  assert.equal(line.split("\n").length, 2);
  assert.deepEqual(JSON.parse(line), {
    protocol: PROTOCOL,
    id: "request-1",
    ok: true,
    result: { accepted: true },
  });
});
