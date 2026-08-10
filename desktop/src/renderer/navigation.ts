export type ViewId = "task" | "automation" | "settings";

export const NAV_ITEMS = [
  { id: "task", label: "对话", mark: "◉" },
  { id: "automation", label: "自动化", mark: "↻" },
  { id: "settings", label: "设置", mark: "⚙" },
] as const satisfies readonly { id: ViewId; label: string; mark: string }[];
