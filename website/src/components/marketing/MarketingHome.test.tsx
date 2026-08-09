import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { MarketingHome } from './MarketingHome';

describe('MarketingHome', () => {
  it('renders exactly three product sections with localized navigation', () => {
    const { container } = render(<MarketingHome locale="zh-CN" />);

    expect(container.querySelectorAll('main > section')).toHaveLength(3);
    expect(container.querySelector('#hero')).toBeInTheDocument();
    expect(container.querySelector('#product')).toBeInTheDocument();
    expect(container.querySelector('#workbench')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '文档' })).toHaveAttribute('href', '/docs');
    expect(screen.getByRole('link', { name: 'English' })).toHaveAttribute('href', '/en');
  });
});
