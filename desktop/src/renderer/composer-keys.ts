export type ComposerKeyAction = "send" | "newline" | "ignore";

export function resolveComposerKeyAction(input: {
  key: string;
  shiftKey: boolean;
  isComposing: boolean;
}): ComposerKeyAction {
  if (input.key !== "Enter") {
    return "ignore";
  }
  if (input.isComposing) {
    return "ignore";
  }
  return input.shiftKey ? "newline" : "send";
}
