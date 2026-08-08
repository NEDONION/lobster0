/** Compact terminal palette and safe ANSI helpers for MiniClaw. */

import type { EditorTheme, MarkdownTheme, SelectListTheme } from "@earendil-works/pi-tui";

type Style = (text: string) => string;

function ansi(open: number, close: number): Style {
  return (text) => `\u001b[${open}m${text}\u001b[${close}m`;
}

export const palette = {
  primary: ansi(38, 39),
  blue: (text: string) => `\u001b[38;5;39m${text}\u001b[39m`,
  green: (text: string) => `\u001b[38;5;78m${text}\u001b[39m`,
  amber: (text: string) => `\u001b[38;5;214m${text}\u001b[39m`,
  red: (text: string) => `\u001b[38;5;203m${text}\u001b[39m`,
  cyan: (text: string) => `\u001b[38;5;44m${text}\u001b[39m`,
  muted: ansi(2, 22),
  bold: ansi(1, 22),
  underline: ansi(4, 24),
};

export const selectListTheme: SelectListTheme = {
  selectedPrefix: palette.amber,
  selectedText: palette.bold,
  description: palette.muted,
  scrollInfo: palette.muted,
  noMatch: palette.red,
};

export const editorTheme: EditorTheme = {
  borderColor: palette.amber,
  selectList: selectListTheme,
};

export const markdownTheme: MarkdownTheme = {
  heading: palette.bold,
  link: palette.cyan,
  linkUrl: palette.muted,
  code: palette.amber,
  codeBlock: (text) => text,
  codeBlockBorder: palette.muted,
  quote: palette.muted,
  quoteBorder: palette.muted,
  hr: palette.muted,
  listBullet: palette.blue,
  bold: palette.bold,
  italic: ansi(3, 23),
  strikethrough: ansi(9, 29),
  underline: palette.underline,
  codeBlockIndent: "  ",
};

/** Removes control bytes that could rewrite terminal state while preserving layout. */
export function terminalSafe(value: string): string {
  let safe = "";
  for (const character of value) {
    const code = character.codePointAt(0) ?? 0;
    if (character === "\n" || character === "\t" || (code >= 0x20 && !(code >= 0x7f && code <= 0x9f))) {
      safe += character;
    }
  }
  return safe;
}
