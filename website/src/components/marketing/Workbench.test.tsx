import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { marketingCopy } from '@/content/site';

import { Workbench } from './Workbench';

describe('Workbench', () => {
  it('uses two animated flow diagrams and one multi-channel diagram', () => {
    render(<Workbench locale="zh-CN" workflows={marketingCopy['zh-CN'].workflows} />);

    expect(screen.getByRole('tab', { name: /SAFE/ })).toBeInTheDocument();
    expect(screen.getByRole('list', { name: /所有者决定/ })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /CLI/ })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /多入口/ })).toBeInTheDocument();
    expect(screen.getByText('真实执行链路')).toBeInTheDocument();
    expect(screen.getByText('exact argv')).toBeInTheDocument();
  });

  it('keeps the close inside the third section with real install actions', () => {
    const { container } = render(
      <Workbench locale="en" workflows={marketingCopy.en.workflows} />,
    );

    expect(container.querySelector('.quick-start-close')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Read the install guide' })).toHaveAttribute(
      'href',
      '/en/docs/getting-started',
    );
    expect(screen.getByText(/git clone https:\/\/github.com\/NEDONION\/miniclaw.git/)).toBeInTheDocument();
  });
});
