import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";
import test, { type TestContext } from "node:test";

import { ActionExecutor, BrowserActionError } from "../dist/actions.js";
import { SessionManager } from "../dist/sessions.js";

const executable = [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
].find(existsSync);
const fixtureRoot = resolve(import.meta.dirname, "../../tests/fixtures/browser-site");

test(
  "stages screenshot with bounded PNG dimensions and an opaque name",
  { skip: executable === undefined },
  async (t) => {
    const fixture = await setup(t, 5 * 1024 * 1024);
    const result = await fixture.actions.execute(request("screenshot", { full_page: false }));
    const artifact = result.artifact as Record<string, unknown>;
    const path = String(artifact.staging_path);
    const bytes = await readFile(path);

    assert.equal(artifact.declared_media_type, "image/png");
    assert.equal(artifact.source, "browser_screenshot");
    assert.match(basename(path), /^[0-9a-f-]{36}\.png$/);
    assert.deepEqual(bytes.subarray(0, 8), Buffer.from("89504e470d0a1a0a", "hex"));
    assert.equal(typeof artifact.width, "number");
    assert.equal(typeof artifact.height, "number");
  },
);

test(
  "ignores traversal filename and removes an oversized download",
  { skip: executable === undefined },
  async (t) => {
    const fixture = await setup(t, 64);
    const snapshot = snapshotOf(await fixture.actions.execute(request("snapshot", {})));
    const link = snapshot.elements.find((element) => element.name === "下载文本");
    assert.ok(link);
    const result = await fixture.actions.execute(
      request("click", target(snapshot, link)),
    );
    const artifact = result.artifact as Record<string, unknown>;
    const path = String(artifact.staging_path);

    assert.equal(await readFile(path, "utf8"), "safe download");
    assert.equal(basename(path).includes("evil"), false);
    const before = await readdir(fixture.staging);

    await fixture.page.evaluate(() => {
      const link = document.createElement("a");
      link.id = "large-download";
      link.textContent = "下载大文件";
      link.download = "../../large.txt";
      link.href = `data:text/plain,${"x".repeat(1024)}`;
      document.body.append(link);
    });
    const next = snapshotOf(await fixture.actions.execute(request("snapshot", {})));
    const large = next.elements.find((element) => element.name === "下载大文件");
    assert.ok(large);
    await assert.rejects(
      fixture.actions.execute(request("click", target(next, large))),
      (error: unknown) =>
        error instanceof BrowserActionError && error.code === "browser_artifact_too_large",
    );
    assert.deepEqual(await readdir(fixture.staging), before);
  },
);

async function setup(t: TestContext, maxArtifactBytes: number) {
  const root = await mkdtemp(join(tmpdir(), "miniclaw-browser-downloads-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const profile = join(root, "profile");
  const staging = join(root, "staging");
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
  return {
    actions: new ActionExecutor(manager, {
      maxSnapshotChars: 20_000,
      stagingRoot: staging,
      maxArtifactBytes,
    }),
    page,
    staging,
  };
}

function snapshotOf(result: Record<string, unknown>) {
  return result.snapshot as {
    generation: string;
    elements: Array<{ ref: string; role: string; name: string }>;
  };
}

function target(
  snapshot: ReturnType<typeof snapshotOf>,
  element: { ref: string; role: string },
) {
  return {
    origin: "null",
    generation: snapshot.generation,
    ref: element.ref,
    role: element.role,
  };
}

function request(action: string, params: Record<string, unknown>) {
  return {
    protocol: "miniclaw.browser.v1" as const,
    id: `request-${action}`,
    session_id: "session-1",
    action,
    params,
  } as never;
}
