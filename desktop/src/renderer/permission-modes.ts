import type { PermissionMode } from "@lobster0/pi-tui/protocol";

export interface PermissionModeOption {
  mode: PermissionMode;
  label: string;
  summary: string;
  /** 该模式下 Lobster0 会自行放行本该询问的动作，界面上要显著提示。 */
  risky: boolean;
}

// 文案对应 policy/engine.py 的真实判定，不是想当然的描述：
// - SAFE/SMART 走 _supervised_action，未命中白名单就要审批；
// - AUTOPILOT/YOLO 对可信 Owner 直接 ALLOW；
// - 只有 YOLO 连 HIGH 风险动作也不再询问（CRITICAL 任何模式都拒绝）。
export const PERMISSION_MODE_OPTIONS: readonly PermissionModeOption[] = [
  {
    mode: "safe",
    label: "SAFE",
    summary: "每个命令和网络请求都要你点头，最稳妥。",
    risky: false,
  },
  {
    mode: "smart",
    label: "SMART",
    summary: "已在白名单里的命令和 HTTPS 直接放行，其余仍会询问。",
    risky: false,
  },
  {
    mode: "autopilot",
    label: "AUTOPILOT",
    summary: "常规动作不再询问；高风险动作仍会停下来等你确认。",
    risky: true,
  },
  {
    mode: "yolo",
    label: "YOLO",
    summary: "连高风险动作也不再询问，只保留最危险动作的硬拒绝。",
    risky: true,
  },
];

export function permissionModeLabel(mode: string): string {
  const option = PERMISSION_MODE_OPTIONS.find((item) => item.mode === mode);
  // Core 未来新增模式时不能显示成空白。
  return option?.label ?? mode.toUpperCase();
}

export function permissionModeSummary(mode: string): string | null {
  return PERMISSION_MODE_OPTIONS.find((item) => item.mode === mode)?.summary ?? null;
}
