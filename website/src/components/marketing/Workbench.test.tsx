import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { marketingCopy } from '@/content/site';

import { Workbench } from './Workbench';

describe('Workbench', () => {
  it('shows the three boundary-moment scenarios', () => {
    render(<Workbench locale="zh-CN" workflows={marketingCopy['zh-CN'].workflows} />);

    expect(screen.getByRole('tab', { name: /审批被拒/ })).toBeInTheDocument();
    expect(screen.getByRole('list', { name: /终止/ })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /执行失败/ })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /故障隔离/ })).toBeInTheDocument();
    expect(screen.getByText('01 / 边界时刻')).toBeInTheDocument();
    expect(screen.getByText('rm -rf /tmp/*')).toBeInTheDocument();
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
    expect(screen.getByText(/git clone https:\/\/github.com\/NEDONION\/lobster0.git/)).toBeInTheDocument();
  });
});
