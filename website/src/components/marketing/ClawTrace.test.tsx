import { render, screen, waitFor } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { marketingCopy } from '@/content/site';

import { ClawTrace } from './ClawTrace';

function setReducedMotion(matches: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn().mockImplementation(() => ({
      addEventListener: vi.fn(),
      matches,
      removeEventListener: vi.fn(),
    })),
  });
}

describe('ClawTrace', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the complete runtime path as an ordered list', () => {
    setReducedMotion(false);
    render(<ClawTrace copy={marketingCopy.en.trace} />);

    expect(screen.getByRole('list', { name: 'Claw Trace' }).tagName).toBe('OL');
    for (const step of marketingCopy.en.trace.steps) {
      expect(screen.getByText(step.event)).toBeInTheDocument();
    }
    const source = readFileSync(resolve('src/components/marketing/ClawTrace.tsx'), 'utf8');
    expect(source).not.toContain('setInterval');
  });

  it('shows the final state immediately under reduced motion', async () => {
    setReducedMotion(true);
    render(<ClawTrace copy={marketingCopy.en.trace} />);

    await waitFor(() => {
      expect(screen.getByText('RESULT_DELIVERED').closest('li')).toHaveAttribute(
        'aria-current',
        'step',
      );
    });
  });
});
