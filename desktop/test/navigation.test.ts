import { describe, expect, it } from "vitest";

import { NAV_ITEMS } from "../src/renderer/navigation";

describe("desktop navigation", () => {
  it("exposes exactly the four approved views", () => {
    expect(NAV_ITEMS.map((item) => item.id)).toEqual([
      "home",
      "task",
      "automation",
      "settings",
    ]);
  });
});
