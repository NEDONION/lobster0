import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_FRAME_BYTES,
  NdjsonDecoder,
  ProtocolClientError,
  encodeRequest,
} from "../dist/protocol.js";

test("fragmented UTF-8 and multiple lines decode to complete server frames", () => {
  const decoder = new NdjsonDecoder();
  const bytes = Buffer.from(
    '{"v":1,"type":"event.model_text_delta","payload":{"turn_id":1,"text":"你好"}}\n' +
      '{"v":1,"id":"r1","type":"response.ok","payload":{}}\n',
  );
  const splitInsideChinese = bytes.indexOf(Buffer.from("你")) + 1;

  assert.deepEqual(decoder.push(bytes.subarray(0, splitInsideChinese)), []);
  const frames = decoder.push(bytes.subarray(splitInsideChinese));

  assert.equal(frames.length, 2);
  assert.equal(frames[0]?.type, "event.model_text_delta");
  assert.deepEqual(frames[0]?.payload, { turn_id: 1, text: "你好" });
  assert.equal(frames[1]?.id, "r1");
});

test("malformed and over-limit server frames fail with stable client codes", () => {
  const invalidUtf8 = new NdjsonDecoder();
  assert.throws(
    () => invalidUtf8.push(Buffer.from([0x7b, 0x22, 0x78, 0x22, 0x3a, 0x22, 0xff, 0x22, 0x7d, 0x0a])),
    (error: unknown) => error instanceof ProtocolClientError && error.code === "invalid_encoding",
  );

  const malformed = new NdjsonDecoder();
  assert.throws(
    () => malformed.push(Buffer.from("{bad}\n")),
    (error: unknown) => error instanceof ProtocolClientError && error.code === "invalid_json",
  );

  const oversized = new NdjsonDecoder();
  assert.throws(
    () => oversized.push(Buffer.alloc(MAX_FRAME_BYTES + 1, 0x20)),
    (error: unknown) => error instanceof ProtocolClientError && error.code === "frame_too_large",
  );
});

test("client requests use one compact UTF-8 NDJSON line", () => {
  const line = encodeRequest("req-1", "turn.start", {
    session_key: "default",
    text: "你好\nMiniClaw",
  });

  assert.equal(line.endsWith("\n"), true);
  assert.equal(line.split("\n").length, 2);
  assert.deepEqual(JSON.parse(line), {
    v: 1,
    id: "req-1",
    type: "turn.start",
    payload: { session_key: "default", text: "你好\nMiniClaw" },
  });
});

test("Desktop session queries use the shared versioned request union", () => {
  assert.equal(
    JSON.parse(encodeRequest("sessions-1", "session.list", { limit: 20 })).type,
    "session.list",
  );
  assert.deepEqual(
    JSON.parse(encodeRequest("history-1", "session.history", {
      session_key: "task-1",
      limit: 100,
    })).payload,
    { session_key: "task-1", limit: 100 },
  );
});
