import type { Telemetry } from "@lobster0/pi-tui/state";

export interface TelemetryFact {
  /** 完整名称，用作 tooltip 与无障碍说明。 */
  label: string;
  /** 顶栏里显示的紧凑文本。 */
  value: string;
}

function formatDuration(durationMs: number): string {
  if (durationMs >= 60_000) {
    const minutes = Math.floor(durationMs / 60_000);
    const seconds = Math.round((durationMs % 60_000) / 1000);
    return `${minutes}m${seconds}s`;
  }
  // 短耗时保留一位小数，避免几百毫秒的运行被显示成 0s。
  return `${(durationMs / 1000).toFixed(1)}s`;
}

function formatTokens(total: number): string {
  if (total >= 1000) {
    return `${(total / 1000).toFixed(1)}k token`;
  }
  return `${total} token`;
}

/**
 * 把一次运行的 telemetry 整理成顶栏可直接渲染的紧凑事实列表。
 *
 * 只输出已经真实测到的项：还没跑过的回合不显示任何数字，避免让 0 看起来
 * 像是"跑完了但什么都没做"。Token 优先用本回合的 input+output 精确用量，
 * 只有在 Provider 没给出分项用量时才退回上下文估算值。
 */
export function telemetryFacts(telemetry: Telemetry): TelemetryFact[] {
  const facts: TelemetryFact[] = [];

  if (telemetry.durationMs !== null) {
    facts.push({ label: "耗时", value: formatDuration(telemetry.durationMs) });
  }

  const { inputTokens, outputTokens, contextTokens } = telemetry;
  if (inputTokens !== null || outputTokens !== null) {
    facts.push({
      label: "Token",
      value: formatTokens((inputTokens ?? 0) + (outputTokens ?? 0)),
    });
  } else if (contextTokens !== null) {
    facts.push({ label: "Token", value: formatTokens(contextTokens) });
  }

  if (telemetry.toolCalls > 0) {
    facts.push({ label: "工具调用", value: `${telemetry.toolCalls} 次工具` });
  }
  if (telemetry.iterations > 0) {
    facts.push({ label: "模型轮次", value: `${telemetry.iterations} 轮` });
  }
  return facts;
}
