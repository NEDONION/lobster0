interface BrandMarkProps {
  className?: string;
  size?: number;
  title?: string;
}

export function BrandMark({ className, size = 40, title }: BrandMarkProps) {
  return (
    <span
      aria-hidden={title ? undefined : true}
      aria-label={title}
      className={className}
      data-brand-mark
      role={title ? 'img' : undefined}
      style={{ fontSize: size * 0.82, lineHeight: 1 }}
    >
      🦞
    </span>
  );
}
