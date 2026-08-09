import { lookup } from "node:dns/promises";
import { BlockList, isIP } from "node:net";

import type { ElementHandle, Page, Route } from "playwright-core";

import type { BrowserRequest, JsonObject, JsonValue } from "./protocol.js";
import { BrowserLifecycleError } from "./sessions.js";
import {
  BrowserSnapshotError,
  readElementMetadata,
  resolveRef,
  takeSnapshot,
} from "./snapshot.js";

const PROVENANCE = "untrusted_web_content";
const KEYS = new Set([
  "Enter",
  "Space",
  "Escape",
  "Tab",
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "PageUp",
  "PageDown",
  "Home",
  "End",
  "Backspace",
  "Delete",
]);
const SENSITIVE_INPUTS = new Set([
  "password",
  "one-time-code",
  "otp",
  "current-password",
  "new-password",
]);
const BLOCKED = new BlockList();
for (const [network, prefix] of [
  ["0.0.0.0", 8],
  ["10.0.0.0", 8],
  ["100.64.0.0", 10],
  ["127.0.0.0", 8],
  ["169.254.0.0", 16],
  ["172.16.0.0", 12],
  ["192.0.0.0", 24],
  ["192.0.2.0", 24],
  ["192.168.0.0", 16],
  ["198.18.0.0", 15],
  ["198.51.100.0", 24],
  ["203.0.113.0", 24],
  ["224.0.0.0", 4],
  ["240.0.0.0", 4],
] as const) {
  BLOCKED.addSubnet(network, prefix, "ipv4");
}
for (const [network, prefix] of [
  ["::", 128],
  ["::1", 128],
  ["100::", 64],
  ["2001:db8::", 32],
  ["fc00::", 7],
  ["fe80::", 10],
  ["ff00::", 8],
] as const) {
  BLOCKED.addSubnet(network, prefix, "ipv6");
}

interface BrowserSessions {
  reap(): Promise<void>;
  open(sessionId: string): Promise<Page>;
  closeSession(sessionId: string): Promise<void>;
}

export interface ActionExecutorOptions {
  maxSnapshotChars: number;
}

export class BrowserActionError extends Error {
  public constructor(
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "BrowserActionError";
  }
}

export class ActionExecutor {
  readonly #sessions: BrowserSessions;
  readonly #maxSnapshotChars: number;
  readonly #guardedPages = new WeakSet<Page>();

  public constructor(sessions: BrowserSessions, options: ActionExecutorOptions) {
    if (
      !Number.isInteger(options.maxSnapshotChars) ||
      options.maxSnapshotChars < 1000 ||
      options.maxSnapshotChars > 100_000
    ) {
      throw new BrowserActionError("browser_limits_invalid", "Browser limits are invalid");
    }
    this.#sessions = sessions;
    this.#maxSnapshotChars = options.maxSnapshotChars;
  }

  public async execute(request: BrowserRequest): Promise<JsonObject> {
    try {
      return await this.#execute(request);
    } catch (error) {
      if (
        error instanceof BrowserActionError ||
        error instanceof BrowserSnapshotError ||
        error instanceof BrowserLifecycleError
      ) {
        throw new BrowserActionError(error.code, "Browser action failed");
      }
      throw new BrowserActionError("browser_action_failed", "Browser action failed");
    }
  }

  async #execute(request: BrowserRequest): Promise<JsonObject> {
    if (request.action === "close") {
      exactParams(request.params, []);
      await this.#sessions.closeSession(request.session_id);
      return result("close", "closed", null, null);
    }
    if (request.action === "screenshot") {
      throw new BrowserActionError(
        "browser_action_unavailable",
        "Browser screenshot is not available",
      );
    }
    await this.#sessions.reap();
    const page = await this.#sessions.open(request.session_id);
    const before = page.url();
    if (request.action === "open") {
      exactParams(request.params, ["url"]);
      const url = stringParam(request.params, "url", 8192);
      await validatePublicHttps(url);
      await this.#guardPage(page);
      await page.goto(url, { waitUntil: "domcontentloaded", timeout: 20_000 });
      return result("open", "ok", before, page.url());
    }
    if (request.action === "snapshot") {
      exactParams(request.params, ["cursor", "max_chars"], true);
      const cursor = integerParam(request.params, "cursor", 0, 1_000_000, 0);
      const requested = integerParam(
        request.params,
        "max_chars",
        1000,
        this.#maxSnapshotChars,
        this.#maxSnapshotChars,
      );
      const snapshot = await takeSnapshot(page, {
        cursor,
        maxChars: Math.min(requested, this.#maxSnapshotChars),
      });
      return {
        ...result("snapshot", "ok", before, page.url()),
        snapshot: snapshot as unknown as JsonObject,
      };
    }
    if (request.action === "click") {
      exactParams(request.params, ["origin", "generation", "ref", "role"]);
      const element = await this.#element(page, request.params);
      await element.click({ timeout: 10_000 });
      await page.waitForLoadState("domcontentloaded", { timeout: 5_000 }).catch(() => undefined);
      return result("click", "ok", before, page.url());
    }
    if (request.action === "type") {
      exactParams(request.params, [
        "origin",
        "generation",
        "ref",
        "role",
        "input_kind",
        "text",
      ]);
      const element = await this.#element(page, request.params);
      const metadata = await element.evaluate(readElementMetadata);
      const claimedKind = stringParam(request.params, "input_kind", 64).toLowerCase();
      const actualKind = String(metadata.state.input_kind ?? "").toLowerCase();
      if (SENSITIVE_INPUTS.has(actualKind)) {
        throw new BrowserActionError(
          "browser_sensitive_input",
          "Sensitive browser input is denied",
        );
      }
      if (claimedKind !== actualKind) {
        throw new BrowserActionError("browser_target_mismatch", "Browser target changed");
      }
      const text = stringParam(request.params, "text", 20_000, true);
      await element.fill(text, { timeout: 10_000 });
      return result("type", "ok", before, page.url());
    }
    if (request.action === "press") {
      exactParams(request.params, ["origin", "generation", "ref", "role", "key"]);
      const key = stringParam(request.params, "key", 32);
      if (!KEYS.has(key)) {
        throw new BrowserActionError("browser_key_denied", "Browser key is denied");
      }
      const element = await this.#element(page, request.params);
      await element.press(key);
      return result("press", "ok", before, page.url());
    }
    if (request.action === "scroll") {
      exactParams(request.params, ["delta_y"]);
      const delta = integerParam(request.params, "delta_y", -10_000, 10_000);
      if (delta === 0) {
        throw new BrowserActionError("browser_params_invalid", "Browser params are invalid");
      }
      await page.mouse.wheel(0, delta);
      return result("scroll", "ok", before, page.url());
    }
    throw new BrowserActionError("browser_action_unknown", "Browser action is unknown");
  }

  async #element(page: Page, params: JsonObject): Promise<ElementHandle> {
    const actualOrigin = new URL(page.url()).origin;
    if (stringParam(params, "origin", 255) !== actualOrigin) {
      throw new BrowserActionError("browser_origin_mismatch", "Browser origin changed");
    }
    const generation = stringParam(params, "generation", 128);
    const ref = stringParam(params, "ref", 16);
    const role = stringParam(params, "role", 64);
    const element = await resolveRef(page, generation, ref);
    const metadata = await element.evaluate(readElementMetadata);
    if (metadata.role !== role) {
      throw new BrowserActionError("browser_target_mismatch", "Browser target changed");
    }
    return element;
  }

  async #guardPage(page: Page): Promise<void> {
    if (this.#guardedPages.has(page)) return;
    await page.route("**/*", guardRoute);
    page.on("popup", (popup) => {
      void popup.close().catch(() => undefined);
    });
    this.#guardedPages.add(page);
  }
}

async function guardRoute(route: Route): Promise<void> {
  try {
    await validatePublicHttps(route.request().url());
    await route.continue();
  } catch {
    await route.abort("blockedbyclient");
  }
}

async function validatePublicHttps(value: string): Promise<void> {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new BrowserActionError("browser_url_invalid", "Browser URL is invalid");
  }
  if (
    url.protocol !== "https:" ||
    url.username !== "" ||
    url.password !== "" ||
    url.hash !== "" ||
    url.port !== ""
  ) {
    throw new BrowserActionError("browser_https_required", "Browser URL must use HTTPS");
  }
  let addresses: Array<{ address: string; family: number }>;
  const hostname = url.hostname.startsWith("[") ? url.hostname.slice(1, -1) : url.hostname;
  if (isIP(hostname) !== 0) {
    addresses = [{ address: hostname, family: isIP(hostname) }];
  } else {
    try {
      addresses = await lookup(hostname, { all: true, verbatim: true });
    } catch {
      throw new BrowserActionError("browser_dns_failed", "Browser DNS failed");
    }
  }
  if (
    addresses.length === 0 ||
    addresses.some(({ address, family }) => isBlocked(address, family))
  ) {
    throw new BrowserActionError("browser_non_public_address", "Browser target is not public");
  }
}

function isBlocked(address: string, family: number): boolean {
  const mapped = address.toLowerCase().match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/)?.[1];
  if (mapped !== undefined) return BLOCKED.check(mapped, "ipv4");
  return BLOCKED.check(address, family === 6 ? "ipv6" : "ipv4");
}

function result(
  action: string,
  status: string,
  before: string | null,
  after: string | null,
): JsonObject {
  return {
    action,
    status,
    url_before: before,
    url_after: after,
    provenance: PROVENANCE,
  };
}

function exactParams(params: JsonObject, keys: string[], optional = false): void {
  const actual = Object.keys(params).sort();
  const allowed = [...keys].sort();
  if (
    actual.some((key) => !allowed.includes(key)) ||
    (!optional && allowed.some((key) => !actual.includes(key)))
  ) {
    throw new BrowserActionError("browser_params_invalid", "Browser params are invalid");
  }
}

function stringParam(
  params: JsonObject,
  name: string,
  maximum: number,
  allowEmpty = false,
): string {
  const value = params[name];
  if (
    typeof value !== "string" ||
    (!allowEmpty && value.length === 0) ||
    value.length > maximum ||
    value.includes("\0")
  ) {
    throw new BrowserActionError("browser_params_invalid", "Browser params are invalid");
  }
  return value;
}

function integerParam(
  params: JsonObject,
  name: string,
  minimum: number,
  maximum: number,
  fallback?: number,
): number {
  const value: JsonValue | undefined = params[name];
  if (value === undefined && fallback !== undefined) return fallback;
  if (typeof value !== "number" || !Number.isInteger(value) || value < minimum || value > maximum) {
    throw new BrowserActionError("browser_params_invalid", "Browser params are invalid");
  }
  return value;
}
