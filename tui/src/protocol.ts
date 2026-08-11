/** Versioned NDJSON types shared with the Python Core Bridge. */

export const PROTOCOL_VERSION = 1 as const;
export const MAX_FRAME_BYTES = 2 * 1024 * 1024;
const UTF8_DECODER = new TextDecoder("utf-8", { fatal: true });

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export type RequestType =
  | "client.hello"
  | "turn.start"
  | "turn.cancel"
  | "approval.resolve"
  | "permissions.set"
  | "memory.command"
  | "session.new"
  | "session.list"
  | "session.history"
  | "automation.list"
  | "automation.pause"
  | "automation.resume"
  | "automation.run"
  | "automation.cancel"
  | "automation.runs"
  | "automation.halt"
  | "automation.unhalt"
  | "automation.create"
  | "bridge.shutdown";

export type PermissionMode = "safe" | "smart" | "autopilot" | "yolo";

export function isPermissionMode(value: unknown): value is PermissionMode {
  return value === "safe" || value === "smart" || value === "autopilot" || value === "yolo";
}

export interface ServerFrame {
  v: 1;
  id?: string;
  type: string;
  payload: Record<string, JsonValue>;
}

export class ProtocolClientError extends Error {
  public readonly code: string;

  public constructor(code: string, message: string) {
    super(message);
    this.name = "ProtocolClientError";
    this.code = code;
  }
}

/** Incrementally decodes complete UTF-8 NDJSON frames without splitting code points. */
export class NdjsonDecoder {
  private pending = Buffer.alloc(0);

  public push(chunk: Uint8Array): ServerFrame[] {
    if (chunk.byteLength > 0) {
      this.pending = Buffer.concat([this.pending, Buffer.from(chunk)]);
    }
    if (this.pending.byteLength > MAX_FRAME_BYTES && this.pending.indexOf(0x0a) === -1) {
      throw new ProtocolClientError("frame_too_large", "Bridge frame exceeds 2 MiB");
    }

    const frames: ServerFrame[] = [];
    while (true) {
      const newline = this.pending.indexOf(0x0a);
      if (newline === -1) {
        break;
      }
      const line = this.pending.subarray(0, newline);
      this.pending = this.pending.subarray(newline + 1);
      if (line.byteLength > MAX_FRAME_BYTES) {
        throw new ProtocolClientError("frame_too_large", "Bridge frame exceeds 2 MiB");
      }
      if (line.byteLength === 0) {
        throw new ProtocolClientError("invalid_json", "Bridge returned an empty frame");
      }
      frames.push(parseServerFrame(line));
    }
    return frames;
  }

  public finish(): void {
    if (this.pending.byteLength !== 0) {
      throw new ProtocolClientError("truncated_frame", "Bridge closed during a frame");
    }
  }
}

/** Encodes one validated client request as compact UTF-8 NDJSON. */
export function encodeRequest(
  id: string,
  type: RequestType,
  payload: Record<string, JsonValue>,
): string {
  if (!/^[A-Za-z0-9._:-]{1,128}$/.test(id)) {
    throw new ProtocolClientError("invalid_request_id", "Client request id is invalid");
  }
  const encoded = `${JSON.stringify({ v: PROTOCOL_VERSION, id, type, payload })}\n`;
  if (Buffer.byteLength(encoded, "utf8") > MAX_FRAME_BYTES) {
    throw new ProtocolClientError("frame_too_large", "Client frame exceeds 2 MiB");
  }
  return encoded;
}

function parseServerFrame(line: Uint8Array): ServerFrame {
  let text: string;
  try {
    text = UTF8_DECODER.decode(line);
  } catch {
    throw new ProtocolClientError("invalid_encoding", "Bridge frame must use UTF-8");
  }
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new ProtocolClientError("invalid_json", "Bridge returned invalid JSON");
  }
  if (!isRecord(value) || value.v !== PROTOCOL_VERSION) {
    throw new ProtocolClientError("invalid_envelope", "Bridge envelope is invalid");
  }
  if (typeof value.type !== "string" || !/^[a-z][a-z0-9_.]{0,63}$/.test(value.type)) {
    throw new ProtocolClientError("invalid_envelope", "Bridge frame type is invalid");
  }
  if (!isRecord(value.payload)) {
    throw new ProtocolClientError("invalid_envelope", "Bridge payload is invalid");
  }
  if (value.id !== undefined && typeof value.id !== "string") {
    throw new ProtocolClientError("invalid_envelope", "Bridge response id is invalid");
  }
  const frame: ServerFrame = {
    v: PROTOCOL_VERSION,
    type: value.type,
    payload: value.payload as Record<string, JsonValue>,
  };
  if (typeof value.id === "string") {
    frame.id = value.id;
  }
  return frame;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
