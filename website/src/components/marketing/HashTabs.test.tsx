import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { HashTabs } from './HashTabs';

const items = [
  { id: 'runtime', label: 'Runtime', panel: <p>Runtime panel</p> },
  { id: 'safety', label: 'Safety', panel: <p>Safety panel</p> },
] as const;

describe('HashTabs', () => {
  afterEach(() => {
    history.replaceState(null, '', '/');
  });

  it('restores a known hash and exposes stable tab relationships', async () => {
    history.replaceState(null, '', '#safety');
    render(<HashTabs ariaLabel="Capabilities" items={items} />);

    const safety = screen.getByRole('tab', { name: 'Safety' });
    await waitFor(() => expect(safety).toHaveAttribute('aria-selected', 'true'));
    expect(safety).toHaveAttribute('id', 'safety-tab');
    expect(safety).toHaveAttribute('aria-controls', 'safety-panel');
    expect(screen.getByRole('tabpanel')).toHaveAttribute('id', 'safety-panel');
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', 'safety-tab');
  });

  it('supports arrow, Home, and End keyboard navigation', () => {
    render(<HashTabs ariaLabel="Capabilities" items={items} />);
    const runtime = screen.getByRole('tab', { name: 'Runtime' });
    const safety = screen.getByRole('tab', { name: 'Safety' });

    runtime.focus();
    fireEvent.keyDown(runtime, { key: 'ArrowRight' });
    expect(safety).toHaveFocus();
    expect(safety).toHaveAttribute('aria-selected', 'true');
    fireEvent.keyDown(safety, { key: 'Home' });
    expect(runtime).toHaveFocus();
    fireEvent.keyDown(runtime, { key: 'End' });
    expect(safety).toHaveFocus();
    fireEvent.keyDown(safety, { key: 'ArrowRight' });
    expect(runtime).toHaveFocus();
  });

  it('falls back for unknown hashes without rewriting the URL', async () => {
    history.replaceState(null, '', '#unknown');
    render(<HashTabs ariaLabel="Capabilities" items={items} />);

    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Runtime' })).toHaveAttribute(
        'aria-selected',
        'true',
      ),
    );
    expect(location.hash).toBe('#unknown');
  });

  it('responds to hashchange events', async () => {
    render(<HashTabs ariaLabel="Capabilities" items={items} />);
    history.pushState(null, '', '#safety');
    window.dispatchEvent(new HashChangeEvent('hashchange'));

    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Safety' })).toHaveAttribute(
        'aria-selected',
        'true',
      ),
    );
  });
});
