export type ViewId = "home" | "task" | "automation" | "settings";

export const NAV_ITEMS = [
  { id: "home", label: "首页", mark: "⌂" },
  { id: "task", label: "任务", mark: "◉" },
  { id: "automation", label: "自动化", mark: "↻" },
  { id: "settings", label: "设置", mark: "⚙" },
] as const satisfies readonly { id: ViewId; label: string; mark: string }[];
