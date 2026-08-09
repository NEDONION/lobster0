import { Buffer } from "node:buffer";

export const PROTOCOL = "miniclaw.browser.v1";
export const MAX_FRAME_BYTES = 1024 * 1024;

const IDENTIFIER = /^[A-Za-z0-9._:-]{1,128}$/;
const ACTIONS = new Set([
  "open",
  "snapshot",
  "click",
  "type",
  "press",
  "scroll",
  "screenshot",
  "close",
] as const);

export type JsonValue = null | boolean | number | string | JsonValue[] | JsonObject;
export type JsonObject = { [key: string]: JsonValue };
export type BrowserActionKind =
  | "open"
  | "snapshot"
  | "click"
  | "type"
  | "press"
  | "scroll"
  | "screenshot"
  | "close";

export interface BrowserRequest {
  protocol: typeof PROTOCOL;
  id: string;
  session_id: string;
  action: BrowserActionKind;
  params: JsonObject;
}

export type BrowserResponse =
  | {
      protocol: typeof PROTOCOL;
      id: string;
      ok: true;
      result: JsonObject;
    }
  | {
      protocol: typeof PROTOCOL;
      id: string;
      ok: false;
      error: { code: string; message: string };
    };

export class ProtocolError extends Error {
  public constructor(
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ProtocolError";
  }
}

export function parseRequest(raw: string | Buffer): BrowserRequest {
  const bytes = typeof raw === "string" ? Buffer.from(raw, "utf8") : raw;
  if (bytes.byteLength > MAX_FRAME_BYTES) {
    throw new ProtocolError("frame_too_large", "Browser request exceeds 1 MiB");
  }
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new ProtocolError("invalid_encoding", "Browser request must use UTF-8");
  }
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new ProtocolError("invalid_json", "Browser request must be valid JSON");
  }
  if (!isObject(value) || !hasExactKeys(value, ["protocol", "id", "session_id", "action", "params"])) {
    throw new ProtocolError("invalid_envelope", "Browser request envelope is invalid");
  }
  if (value.protocol !== PROTOCOL) {
    throw new ProtocolError("unsupported_version", "Browser protocol is unsupported");
  }
  if (typeof value.id !== "string" || !IDENTIFIER.test(value.id)) {
    throw new ProtocolError("invalid_request_id", "Browser request ID is invalid");
  }
  if (typeof value.session_id !== "string" || !IDENTIFIER.test(value.session_id)) {
    throw new ProtocolError("invalid_session", "Browser session ID is invalid");
  }
  if (typeof value.action !== "string" || !ACTIONS.has(value.action as BrowserActionKind)) {
    throw new ProtocolError("unknown_action", "Browser action is unsupported");
  }
  if (!isObject(value.params)) {
    throw new ProtocolError("invalid_params", "Browser params must be an object");
  }
  return value as unknown as BrowserRequest;
}

export function encodeResponse(response: BrowserResponse): string {
  const line = `${JSON.stringify(response)}\n`;
  if (Buffer.byteLength(line, "utf8") > MAX_FRAME_BYTES) {
    throw new ProtocolError("frame_too_large", "Browser response exceeds 1 MiB");
  }
  return line;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, expected: string[]): boolean {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
}
