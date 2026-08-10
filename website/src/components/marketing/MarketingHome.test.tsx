import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { MarketingHome } from './MarketingHome';

describe('MarketingHome', () => {
  it('renders exactly three product sections with localized navigation', async () => {
    const user = userEvent.setup();
    const { container } = render(<MarketingHome locale="zh-CN" />);

    expect(container.querySelectorAll('main > section')).toHaveLength(3);
    expect(container.querySelector('#hero')).toBeInTheDocument();
    expect(container.querySelector('#product')).toBeInTheDocument();
    expect(container.querySelector('#workbench')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '文档' })).toHaveAttribute('href', '/docs');

    await user.click(screen.getByRole('button', { name: '切换语言' }));
    expect(screen.getByRole('option', { name: 'English' })).toHaveAttribute('href', '/en');
  });

  it('uses the three-arrow brand and localized surface cards', () => {
    const { container } = render(<MarketingHome locale="zh-CN" />);

    expect(container.querySelectorAll('[data-brand-mark]')).toHaveLength(2);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('你的本地行动助手');
    expect(screen.getByText('本地界面')).toBeInTheDocument();
    expect(screen.getByText('工作入口')).toBeInTheDocument();
    expect(screen.getByText('移动入口')).toBeInTheDocument();
    expect(screen.getByText('社区入口')).toBeInTheDocument();
  });

  it('keeps Chinese structural labels around technical identifiers', async () => {
    const user = userEvent.setup();
    render(<MarketingHome locale="zh-CN" />);

    expect(screen.getByText('01 / 核心循环')).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: /自动化/ }));
    expect(screen.getByText('默认关闭')).toBeInTheDocument();
  });
});
