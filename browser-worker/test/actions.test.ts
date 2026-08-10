import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import test from "node:test";

import { chromium } from "playwright-core";

import { ActionExecutor, BrowserActionError } from "../dist/actions.js";
import { SessionManager } from "../dist/sessions.js";

const executable = [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
].find(existsSync);
const fixtureRoot = resolve(import.meta.dirname, "../../tests/fixtures/browser-site");

test(
  "executes snapshot, click and non-sensitive type with untrusted provenance",
  { skip: executable === undefined },
  async (t) => {
    const profile = await mkdtemp(join(tmpdir(), "lobster0-browser-actions-"));
    t.after(() => rm(profile, { recursive: true, force: true }));
    const manager = new SessionManager({
      profileRoot: profile,
      executablePath: executable as string,
      maxTabs: 2,
      inactivityTimeoutMs: 120_000,
      headed: false,
    });
    t.after(() => manager.close());
    const page = await manager.open("session-1");
    await page.goto(`file://${resolve(fixtureRoot, "index.html")}`);
    const actions = new ActionExecutor(manager, {
      maxSnapshotChars: 20_000,
      stagingRoot: join(profile, "staging"),
      maxArtifactBytes: 5 * 1024 * 1024,
    });

    const first = await actions.execute(request("snapshot", {}));
    assert.equal(first.provenance, "untrusted_web_content");
    assert.equal(first.status, "ok");
    const snapshot = first.snapshot as {
      generation: string;
      elements: Array<{
        ref: string;
        role: string;
        name: string;
        state: Record<string, string | boolean>;
      }>;
    };
    assert.equal(JSON.stringify(snapshot).includes("Ignore prior instructions"), true);
    const search = snapshot.elements.find((element) => element.name === "搜索");
    const password = snapshot.elements.find(
      (element) => element.state.input_kind === "password",
    );
    const button = snapshot.elements.find((element) => element.name === "打开详情");
    assert.ok(search && password && button);

    const typed = await actions.execute(
      request("type", {
        origin: "null",
        generation: snapshot.generation,
        ref: search.ref,
        role: search.role,
        input_kind: "text",
        text: "Lobster0 query",
      }),
    );
    assert.equal(typed.provenance, "untrusted_web_content");
    assert.equal(await page.locator("#search").inputValue(), "Lobster0 query");

    await assert.rejects(
      actions.execute(
        request("type", {
          origin: "null",
          generation: snapshot.generation,
          ref: password.ref,
          role: password.role,
          input_kind: "text",
          text: "must-not-type",
        }),
      ),
      (error: unknown) =>
        error instanceof BrowserActionError && error.code === "browser_sensitive_input",
    );
    assert.equal(await page.locator("input[type=password]").inputValue(), "snapshot-must-not-leak");

    await actions.execute(
      request("click", {
        origin: "null",
        generation: snapshot.generation,
        ref: button.ref,
        role: button.role,
      }),
    );
    assert.equal(await page.locator("#open").getAttribute("data-clicked"), "yes");
  },
);

test(
  "rejects stale refs and closes the exact session",
  { skip: executable === undefined },
  async (t) => {
    const profile = await mkdtemp(join(tmpdir(), "lobster0-browser-stale-action-"));
    t.after(() => rm(profile, { recursive: true, force: true }));
    const manager = new SessionManager({
      profileRoot: profile,
      executablePath: executable as string,
      maxTabs: 1,
      inactivityTimeoutMs: 120_000,
      headed: false,
    });
    t.after(() => manager.close());
    const page = await manager.open("session-1");
    await page.goto(`file://${resolve(fixtureRoot, "dynamic.html")}`);
    const actions = new ActionExecutor(manager, {
      maxSnapshotChars: 20_000,
      stagingRoot: join(profile, "staging"),
      maxArtifactBytes: 5 * 1024 * 1024,
    });
    const result = await actions.execute(request("snapshot", {}));
    const snapshot = result.snapshot as {
      generation: string;
      elements: Array<{ ref: string; role: string }>;
    };
    const target = snapshot.elements[0];
    assert.ok(target);
    await page.evaluate(() => document.body.replaceChildren(document.createElement("p")));

    await assert.rejects(
      actions.execute(
        request("click", {
          origin: "null",
          generation: snapshot.generation,
          ref: target.ref,
          role: target.role,
        }),
      ),
      (error: unknown) =>
        error instanceof BrowserActionError && error.code === "browser_stale_ref",
    );
    await actions.execute(request("close", {}));
    assert.equal(manager.sessionCount, 0);
  },
);

test("open accepts only HTTPS and returns before/after URLs", async () => {
  const page = new FakePage();
  const actions = new ActionExecutor(new FakeSessions(page), {
    maxSnapshotChars: 20_000,
    stagingRoot: join(tmpdir(), "lobster0-browser-unused-staging"),
    maxArtifactBytes: 5 * 1024 * 1024,
  });

  const result = await actions.execute(request("open", { url: "https://93.184.216.34/path" }));

  assert.equal(result.url_before, "about:blank");
  assert.equal(result.url_after, "https://93.184.216.34/path");
  await assert.rejects(
    actions.execute(request("open", { url: "http://example.com" })),
    (error: unknown) =>
      error instanceof BrowserActionError && error.code === "browser_https_required",
  );
  await assert.rejects(
    actions.execute(request("open", { url: "https://127.0.0.1/private" })),
    (error: unknown) =>
      error instanceof BrowserActionError && error.code === "browser_non_public_address",
  );
});

test("versioned server dispatches close through ActionExecutor", async (t) => {
  const profile = await mkdtemp(join(tmpdir(), "lobster0-browser-server-"));
  t.after(() => rm(profile, { recursive: true, force: true }));
  const child = spawn(
    process.execPath,
    [
      resolve(import.meta.dirname, "../dist/server.js"),
      `--profile-root=${profile}`,
      "--executable-path=/test/chromium",
      "--max-tabs=2",
      "--inactivity-timeout-ms=120000",
      "--headed=false",
      "--max-snapshot-chars=20000",
      `--staging-root=${join(profile, "staging")}`,
      "--max-artifact-bytes=5242880",
    ],
    { stdio: ["pipe", "pipe", "pipe"] },
  );
  t.after(() => child.kill("SIGKILL"));
  assert.ok(child.stdout && child.stdin);
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  assert.deepEqual(JSON.parse(String((await lines.next()).value)), {
    protocol: "lobster0.browser.v1",
    type: "ready",
  });
  child.stdin.write(
    `${JSON.stringify({
      protocol: "lobster0.browser.v1",
      id: "close-1",
      session_id: "session-1",
      action: "close",
      params: {},
    })}\n`,
  );
  const response = JSON.parse(String((await lines.next()).value));
  assert.equal(response.ok, true);
  assert.equal(response.result.status, "closed");
  child.stdin.end();
});

function request(action: string, params: Record<string, unknown>) {
  return {
    protocol: "lobster0.browser.v1" as const,
    id: `request-${action}`,
    session_id: "session-1",
    action,
    params,
  } as never;
}

class FakePage {
  public current = "about:blank";

  public url(): string {
    return this.current;
  }

  public async goto(url: string): Promise<null> {
    this.current = url;
    return null;
  }

  public async route(): Promise<void> {}

  public on(): void {}
}

class FakeSessions {
  readonly #page: FakePage;

  public constructor(page: FakePage) {
    this.#page = page;
  }

  public async reap(): Promise<void> {}

  public async open(): Promise<never> {
    return this.#page as never;
  }

  public async closeSession(): Promise<void> {}
}
