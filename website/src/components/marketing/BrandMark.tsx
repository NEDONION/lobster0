interface BrandMarkProps {
  className?: string;
  size?: number;
  title?: string;
}

export function BrandMark({ className, size = 40, title }: BrandMarkProps) {
  return (
    <span
      aria-hidden={title ? undefined : true}
      className={className}
      data-brand-mark
      role={title ? 'img' : undefined}
      aria-label={title}
      style={{ fontSize: size * 0.82, lineHeight: 1 }}
    >
      🦞
    </span>
  );
}
