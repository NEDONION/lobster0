import { randomUUID } from "node:crypto";
import { lookup } from "node:dns/promises";
import { mkdir, lstat, open, rm } from "node:fs/promises";
import { BlockList, isIP } from "node:net";
import { extname, join, resolve } from "node:path";

import type { Download, ElementHandle, Page, Route } from "playwright-core";

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
  stagingRoot: string;
  maxArtifactBytes: number;
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
  readonly #stagingRoot: string;
  readonly #maxArtifactBytes: number;
  readonly #guardedPages = new WeakSet<Page>();

  public constructor(sessions: BrowserSessions, options: ActionExecutorOptions) {
    if (
      !Number.isInteger(options.maxSnapshotChars) ||
      options.maxSnapshotChars < 1000 ||
      options.maxSnapshotChars > 100_000 ||
      !options.stagingRoot ||
      !Number.isInteger(options.maxArtifactBytes) ||
      options.maxArtifactBytes < 1 ||
      options.maxArtifactBytes > 100 * 1024 * 1024
    ) {
      throw new BrowserActionError("browser_limits_invalid", "Browser limits are invalid");
    }
    this.#sessions = sessions;
    this.#maxSnapshotChars = options.maxSnapshotChars;
    this.#stagingRoot = resolve(options.stagingRoot);
    this.#maxArtifactBytes = options.maxArtifactBytes;
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
      const downloadPromise = page
        .waitForEvent("download", { timeout: 1000 })
        .catch(() => null);
      await element.click({ timeout: 10_000 });
      const download = await downloadPromise;
      if (download !== null) {
        const artifact = await this.#stageDownload(download);
        return { ...result("click", "ok", before, page.url()), artifact };
      }
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
    if (request.action === "screenshot") {
      exactParams(request.params, ["full_page"]);
      const fullPage = booleanParam(request.params, "full_page", false);
      const dimensions = await page.evaluate((full) => {
        const root = document.documentElement;
        return {
          width: full ? Math.max(root.scrollWidth, window.innerWidth) : window.innerWidth,
          height: full ? Math.max(root.scrollHeight, window.innerHeight) : window.innerHeight,
        };
      }, fullPage);
      if (
        dimensions.width < 1 ||
        dimensions.height < 1 ||
        dimensions.width > 16_384 ||
        dimensions.height > 16_384 ||
        dimensions.width * dimensions.height > 64_000_000
      ) {
        throw new BrowserActionError(
          "browser_artifact_dimensions",
          "Browser screenshot dimensions are denied",
        );
      }
      const screenshot = await page.screenshot({
        animations: "disabled",
        caret: "hide",
        fullPage,
        type: "png",
      });
      const artifact = await this.#stageBuffer(screenshot, ".png", {
        declared_media_type: "image/png",
        source: "browser_screenshot",
        width: dimensions.width,
        height: dimensions.height,
      });
      return { ...result("screenshot", "ok", before, page.url()), artifact };
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

  async #stageDownload(download: Download): Promise<JsonObject> {
    const path = join(this.#stagingRoot, `${randomUUID()}.download`);
    await this.#ensureStagingRoot();
    const handle = await open(path, "wx", 0o600);
    let total = 0;
    try {
      const stream = await download.createReadStream();
      if (stream === null) {
        throw new BrowserActionError(
          "browser_download_interrupted",
          "Browser download was interrupted",
        );
      }
      for await (const part of stream) {
        const chunk = Buffer.isBuffer(part) ? part : Buffer.from(part);
        total += chunk.byteLength;
        if (total > this.#maxArtifactBytes) {
          await download.cancel().catch(() => undefined);
          throw new BrowserActionError(
            "browser_artifact_too_large",
            "Browser artifact is too large",
          );
        }
        await handle.write(chunk);
      }
      if ((await download.failure()) !== null) {
        throw new BrowserActionError(
          "browser_download_interrupted",
          "Browser download was interrupted",
        );
      }
      await handle.sync();
      return {
        staging_path: path,
        declared_media_type: downloadMediaType(download.suggestedFilename()),
        source: "browser_download",
      };
    } catch (error) {
      await rm(path, { force: true });
      throw error;
    } finally {
      await handle.close();
    }
  }

  async #stageBuffer(
    content: Buffer,
    extension: string,
    metadata: JsonObject,
  ): Promise<JsonObject> {
    if (content.byteLength > this.#maxArtifactBytes) {
      throw new BrowserActionError(
        "browser_artifact_too_large",
        "Browser artifact is too large",
      );
    }
    await this.#ensureStagingRoot();
    const path = join(this.#stagingRoot, `${randomUUID()}${extension}`);
    const handle = await open(path, "wx", 0o600);
    try {
      await handle.writeFile(content);
      await handle.sync();
    } catch (error) {
      await rm(path, { force: true });
      throw error;
    } finally {
      await handle.close();
    }
    return { staging_path: path, ...metadata };
  }

  async #ensureStagingRoot(): Promise<void> {
    await mkdir(this.#stagingRoot, { mode: 0o700, recursive: true });
    const state = await lstat(this.#stagingRoot);
    if (!state.isDirectory() || state.isSymbolicLink() || (state.mode & 0o077) !== 0) {
      throw new BrowserActionError(
        "browser_staging_unsafe",
        "Browser staging directory is unsafe",
      );
    }
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

function booleanParam(params: JsonObject, name: string, fallback: boolean): boolean {
  const value = params[name] ?? fallback;
  if (typeof value !== "boolean") {
    throw new BrowserActionError("browser_params_invalid", "Browser params are invalid");
  }
  return value;
}

function downloadMediaType(filename: string): string {
  const extension = extname(filename).toLowerCase();
  return (
    {
      ".csv": "text/csv",
      ".jpeg": "image/jpeg",
      ".jpg": "image/jpeg",
      ".json": "application/json",
      ".pdf": "application/pdf",
      ".png": "image/png",
      ".txt": "text/plain",
      ".zip": "application/zip",
    }[extension] ?? "application/octet-stream"
  );
}
