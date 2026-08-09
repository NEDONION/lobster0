import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import type { BrowserContext, Page } from "playwright-core";

import { BrowserLifecycleError, SessionManager } from "../dist/sessions.js";

class FakePage {
  public closed = false;

  public async close(): Promise<void> {
    this.closed = true;
  }
}

class FakeContext {
  public readonly initial = new FakePage();
  public readonly created: FakePage[] = [];
  public closed = false;

  public pages(): Page[] {
    return this.closed ? [] : [this.initial, ...this.created] as unknown as Page[];
  }

  public async newPage(): Promise<Page> {
    const page = new FakePage();
    this.created.push(page);
    return page as unknown as Page;
  }

  public async close(): Promise<void> {
    this.closed = true;
  }
}

test("always launches the exact dedicated profile root", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "miniclaw-browser-profile-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const context = new FakeContext();
  let launchedRoot = "";
  let launchOptions: Record<string, unknown> = {};
  const manager = new SessionManager({
    profileRoot: root,
    executablePath: "/test/chromium",
    maxTabs: 2,
    inactivityTimeoutMs: 120_000,
    headed: true,
    launch: async (profileRoot, options) => {
      launchedRoot = profileRoot;
      launchOptions = options;
      return context as unknown as BrowserContext;
    },
  });
  t.after(() => manager.close());

  await manager.open("session-1");

  assert.equal(launchedRoot, root);
  assert.notEqual(launchedRoot, join(tmpdir(), "personal-browser-profile"));
  assert.equal(launchOptions.serviceWorkers, "block");
  assert.equal(launchOptions.acceptDownloads, true);
});

test("closes idle sessions and their browser context", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "miniclaw-browser-idle-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const context = new FakeContext();
  let now = 0;
  const manager = new SessionManager({
    profileRoot: root,
    executablePath: "/test/chromium",
    maxTabs: 2,
    inactivityTimeoutMs: 120_000,
    headed: false,
    now: () => now,
    launch: async () => context as unknown as BrowserContext,
  });
  t.after(() => manager.close());
  await manager.open("session-1");

  now = 121_000;
  await manager.reap();

  assert.equal(context.initial.closed, true);
  assert.equal(context.closed, true);
  assert.equal(manager.sessionCount, 0);
});

test("profile lock and maxTabs fail closed", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "miniclaw-browser-lock-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const firstContext = new FakeContext();
  const first = new SessionManager({
    profileRoot: root,
    executablePath: "/test/chromium",
    maxTabs: 1,
    inactivityTimeoutMs: 120_000,
    headed: false,
    launch: async () => firstContext as unknown as BrowserContext,
  });
  const second = new SessionManager({
    profileRoot: root,
    executablePath: "/test/chromium",
    maxTabs: 1,
    inactivityTimeoutMs: 120_000,
    headed: false,
    launch: async () => new FakeContext() as unknown as BrowserContext,
  });
  t.after(async () => {
    await second.close();
    await first.close();
  });

  await first.open("session-1");
  await assert.rejects(
    first.open("session-2"),
    (error: unknown) => error instanceof BrowserLifecycleError && error.code === "browser_tab_limit",
  );
  await assert.rejects(
    second.open("session-2"),
    (error: unknown) =>
      error instanceof BrowserLifecycleError && error.code === "browser_profile_locked",
  );
});
