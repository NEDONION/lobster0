import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { CommandCopy } from './CommandCopy';

describe('CommandCopy', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('copies the complete command and announces success', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });

    render(
      <CommandCopy command={'first\nsecond'} label="复制" copiedLabel="已复制" title="启动命令" />,
    );
    fireEvent.click(screen.getByRole('button', { name: '复制' }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith('first\nsecond'));
    expect(await screen.findByText('已复制')).toBeInTheDocument();
    expect(screen.getByText(/first/)).toHaveTextContent('first second');
  });

  it('falls back to the document copy command when Clipboard API rejects', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'));
    const execCommand = vi.fn().mockReturnValue(true);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: execCommand,
    });

    render(<CommandCopy command="uv run miniclaw" label="Copy" copiedLabel="Copied" title="Run" />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));

    await waitFor(() => expect(execCommand).toHaveBeenCalledWith('copy'));
    expect(await screen.findByText('Copied')).toBeInTheDocument();
  });
});
