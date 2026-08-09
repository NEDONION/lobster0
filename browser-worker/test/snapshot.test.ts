import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

import { chromium } from "playwright-core";

import { BrowserSnapshotError, resolveRef, takeSnapshot } from "../dist/snapshot.js";

const executable = [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
].find(existsSync);
const fixtureRoot = resolve(import.meta.dirname, "../../tests/fixtures/browser-site");

test(
  "keeps refs stable and never exposes password values",
  { skip: executable === undefined },
  async () => {
    const browser = await chromium.launch({ executablePath: executable, headless: true });
    try {
      const page = await browser.newPage();
      await page.goto(`file://${resolve(fixtureRoot, "index.html")}`);

      const first = await takeSnapshot(page, { maxChars: 20_000 });
      const second = await takeSnapshot(page, { maxChars: 20_000 });

      assert.deepEqual(
        second.elements.map((element) => element.ref),
        first.elements.map((element) => element.ref),
      );
      assert.equal(second.generation, first.generation);
      assert.equal(JSON.stringify(first).includes("snapshot-must-not-leak"), false);
      assert.equal(first.elements.some((element) => element.name.includes("打开详情")), true);
      assert.equal(first.truncated, false);
    } finally {
      await browser.close();
    }
  },
);

test(
  "rejects stale refs after the DOM generation changes",
  { skip: executable === undefined },
  async () => {
    const browser = await chromium.launch({ executablePath: executable, headless: true });
    try {
      const page = await browser.newPage();
      await page.goto(`file://${resolve(fixtureRoot, "dynamic.html")}`);
      const snapshot = await takeSnapshot(page, { maxChars: 20_000 });
      const target = snapshot.elements.find((element) => element.name === "变更前");
      assert.ok(target);

      await page.evaluate(() => document.body.replaceChildren(document.createElement("p")));

      await assert.rejects(
        resolveRef(page, snapshot.generation, target.ref),
        (error: unknown) =>
          error instanceof BrowserSnapshotError && error.code === "browser_stale_ref",
      );
    } finally {
      await browser.close();
    }
  },
);
