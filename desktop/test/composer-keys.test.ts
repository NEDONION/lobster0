import { describe, expect, it } from "vitest";

import { resolveComposerKeyAction } from "../src/renderer/composer-keys";

describe("resolveComposerKeyAction", () => {
  it("sends on Enter without Shift and outside IME composition", () => {
    expect(resolveComposerKeyAction({ key: "Enter", shiftKey: false, isComposing: false }))
      .toBe("send");
  });

  it("inserts a newline on Shift+Enter", () => {
    expect(resolveComposerKeyAction({ key: "Enter", shiftKey: true, isComposing: false }))
      .toBe("newline");
  });

  it("ignores Enter while an IME composition is in progress", () => {
    expect(resolveComposerKeyAction({ key: "Enter", shiftKey: false, isComposing: true }))
      .toBe("ignore");
  });

  it("ignores non-Enter keys", () => {
    expect(resolveComposerKeyAction({ key: "a", shiftKey: false, isComposing: false }))
      .toBe("ignore");
  });
});
