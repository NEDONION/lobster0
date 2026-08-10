import type { Telemetry } from "@lobster0/pi-tui/state";
import { describe, expect, it } from "vitest";

import { telemetryFacts } from "../src/renderer/telemetry-facts";

function telemetry(overrides: Partial<Telemetry> = {}): Telemetry {
  return {
    contextTokens: null,
    inputTokens: null,
    outputTokens: null,
    toolCalls: 0,
    iterations: 0,
    durationMs: null,
    providerRequestId: null,
    ...overrides,
  };
}

describe("telemetryFacts", () => {
  it("returns nothing before a turn has produced any measurement", () => {
    expect(telemetryFacts(telemetry())).toEqual([]);
  });

  it("shows duration in seconds once it is known", () => {
    const [fact] = telemetryFacts(telemetry({ durationMs: 12_900 }));
    expect(fact?.value).toBe("12.9s");
  });

  it("keeps sub-second durations readable instead of rounding to zero", () => {
    const [fact] = telemetryFacts(telemetry({ durationMs: 420 }));
    expect(fact?.value).toBe("0.4s");
  });

  it("switches to minutes for long runs", () => {
    const [fact] = telemetryFacts(telemetry({ durationMs: 195_000 }));
    expect(fact?.value).toBe("3m15s");
  });

  it("reports tool calls and iterations only when they happened", () => {
    const values = telemetryFacts(telemetry({ toolCalls: 3, iterations: 2 })).map((f) => f.value);
    expect(values).toEqual(["3 次工具", "2 轮"]);
  });

  it("sums input and output tokens when both are known", () => {
    const values = telemetryFacts(
      telemetry({ inputTokens: 1200, outputTokens: 340 }),
    ).map((f) => f.value);
    expect(values).toEqual(["1.5k token"]);
  });

  it("falls back to context tokens when per-turn usage is unavailable", () => {
    const values = telemetryFacts(telemetry({ contextTokens: 800 })).map((f) => f.value);
    expect(values).toEqual(["800 token"]);
  });

  it("prefers exact per-turn usage over the context estimate", () => {
    const values = telemetryFacts(
      telemetry({ contextTokens: 9999, inputTokens: 100, outputTokens: 20 }),
    ).map((f) => f.value);
    expect(values).toEqual(["120 token"]);
  });

  it("orders facts as duration, tokens, tool calls, iterations", () => {
    const labels = telemetryFacts(
      telemetry({ durationMs: 1000, inputTokens: 10, outputTokens: 5, toolCalls: 1, iterations: 1 }),
    ).map((f) => f.label);
    expect(labels).toEqual(["耗时", "Token", "工具调用", "模型轮次"]);
  });
});
