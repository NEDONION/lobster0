'use client';

import { useEffect, useRef, useState } from 'react';

interface CommandCopyProps {
  command: string;
  label: string;
  copiedLabel: string;
  title: string;
}

function copyWithDocument(command: string): boolean {
  const textarea = document.createElement('textarea');
  textarea.value = command;
  textarea.readOnly = true;
  textarea.tabIndex = -1;
  textarea.setAttribute('aria-hidden', 'true');
  textarea.style.position = 'fixed';
  textarea.style.inset = '-9999px auto auto -9999px';
  document.body.append(textarea);
  textarea.select();
  textarea.setSelectionRange(0, command.length);

  const execCommand = Reflect.get(document, 'execCommand');
  const copied =
    typeof execCommand === 'function' && Boolean(Reflect.apply(execCommand, document, ['copy']));
  textarea.remove();
  return copied;
}

export function CommandCopy({ command, label, copiedLabel, title }: CommandCopyProps) {
  const [copied, setCopied] = useState(false);
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (resetTimer.current) clearTimeout(resetTimer.current);
    },
    [],
  );

  async function copyCommand() {
    let didCopy = false;

    try {
      await navigator.clipboard?.writeText(command);
      didCopy = Boolean(navigator.clipboard);
    } catch {
      didCopy = false;
    }

    if (!didCopy) didCopy = copyWithDocument(command);
    if (!didCopy) return;

    setCopied(true);
    if (resetTimer.current) clearTimeout(resetTimer.current);
    resetTimer.current = setTimeout(() => setCopied(false), 2000);
  }

  return (
    <div className="command-copy">
      <div className="command-copy__bar">
        <div className="command-copy__window" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <span>{title}</span>
        <button type="button" onClick={copyCommand}>
          <span aria-live="polite">{copied ? copiedLabel : label}</span>
        </button>
      </div>
      <pre tabIndex={0}>
        <code>{command}</code>
      </pre>
    </div>
  );
}
