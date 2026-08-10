import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { marketingCopy } from '@/content/site';

import { Workbench } from './Workbench';

describe('Workbench', () => {
  it('uses two real screenshots and one multi-channel diagram', () => {
    render(<Workbench locale="zh-CN" workflows={marketingCopy['zh-CN'].workflows} />);

    expect(screen.getByRole('tab', { name: /SAFE/ })).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /审批/ })).toHaveAttribute(
      'src',
      expect.stringContaining('approval'),
    );
    expect(screen.getByRole('tab', { name: /CLI/ })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /多入口/ })).toBeInTheDocument();
    expect(screen.getByText('仓库真实图片')).toBeInTheDocument();
    expect(screen.getByText('可观察步骤')).toBeInTheDocument();
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
