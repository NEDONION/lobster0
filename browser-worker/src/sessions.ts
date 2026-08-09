import { resolve } from "node:path";

import { chromium, type BrowserContext, type Page } from "playwright-core";

import { BrowserLifecycleError, ProfileLock } from "./profile.js";

type LaunchOptions = NonNullable<Parameters<typeof chromium.launchPersistentContext>[1]>;
type Launch = (profileRoot: string, options: LaunchOptions) => Promise<BrowserContext>;

export interface SessionManagerOptions {
  profileRoot: string;
  executablePath: string;
  maxTabs: number;
  inactivityTimeoutMs: number;
  headed: boolean;
  now?: () => number;
  launch?: Launch;
}

interface SessionRecord {
  page: Page;
  touchedAt: number;
}

export { BrowserLifecycleError } from "./profile.js";

export class SessionManager {
  readonly #profileRoot: string;
  readonly #executablePath: string;
  readonly #maxTabs: number;
  readonly #inactivityTimeoutMs: number;
  readonly #headed: boolean;
  readonly #now: () => number;
  readonly #launch: Launch;
  readonly #lock: ProfileLock;
  readonly #sessions = new Map<string, SessionRecord>();
  #context: BrowserContext | undefined;

  public constructor(options: SessionManagerOptions) {
    if (options.maxTabs < 1 || options.inactivityTimeoutMs < 1) {
      throw new BrowserLifecycleError("browser_limits_invalid", "Browser limits are invalid");
    }
    this.#profileRoot = resolve(options.profileRoot);
    this.#executablePath = resolve(options.executablePath);
    this.#maxTabs = options.maxTabs;
    this.#inactivityTimeoutMs = options.inactivityTimeoutMs;
    this.#headed = options.headed;
    this.#now = options.now ?? Date.now;
    this.#launch = options.launch ?? chromium.launchPersistentContext.bind(chromium);
    this.#lock = new ProfileLock(this.#profileRoot);
  }

  public get sessionCount(): number {
    return this.#sessions.size;
  }

  public async open(sessionId: string): Promise<Page> {
    const existing = this.#sessions.get(sessionId);
    if (existing !== undefined) {
      existing.touchedAt = this.#now();
      return existing.page;
    }
    const context = await this.#ensureContext();
    const used = new Set([...this.#sessions.values()].map((record) => record.page));
    const available = context.pages().find((page) => !used.has(page));
    if (available === undefined && context.pages().length >= this.#maxTabs) {
      throw new BrowserLifecycleError("browser_tab_limit", "Browser tab limit reached");
    }
    const page = available ?? (await context.newPage());
    this.#sessions.set(sessionId, { page, touchedAt: this.#now() });
    return page;
  }

  public touch(sessionId: string): void {
    const record = this.#sessions.get(sessionId);
    if (record !== undefined) record.touchedAt = this.#now();
  }

  public async reap(): Promise<void> {
    const now = this.#now();
    for (const [sessionId, record] of this.#sessions) {
      if (now - record.touchedAt <= this.#inactivityTimeoutMs) continue;
      await record.page.close().catch(() => undefined);
      this.#sessions.delete(sessionId);
    }
    if (this.#sessions.size === 0) await this.#closeContext();
  }

  public async closeSession(sessionId: string): Promise<void> {
    const record = this.#sessions.get(sessionId);
    if (record === undefined) return;
    this.#sessions.delete(sessionId);
    await record.page.close().catch(() => undefined);
    if (this.#sessions.size === 0) await this.#closeContext();
  }

  public async close(): Promise<void> {
    this.#sessions.clear();
    await this.#closeContext();
  }

  async #ensureContext(): Promise<BrowserContext> {
    if (this.#context !== undefined) return this.#context;
    try {
      await this.#lock.acquire();
      this.#context = await this.#launch(this.#profileRoot, {
        executablePath: this.#executablePath,
        headless: !this.#headed,
      });
      return this.#context;
    } catch (error) {
      await this.#lock.release();
      throw error;
    }
  }

  async #closeContext(): Promise<void> {
    const context = this.#context;
    this.#context = undefined;
    try {
      if (context !== undefined) await context.close();
    } finally {
      await this.#lock.release();
    }
  }
}
