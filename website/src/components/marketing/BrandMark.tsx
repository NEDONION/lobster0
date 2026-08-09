interface BrandMarkProps {
  className?: string;
  size?: number;
  title?: string;
}

export function BrandMark({ className, size = 40, title }: BrandMarkProps) {
  return (
    <svg
      aria-hidden={title ? undefined : true}
      className={className}
      data-brand-mark
      height={size}
      role={title ? 'img' : undefined}
      viewBox="0 0 64 64"
      width={size}
    >
      {title ? <title>{title}</title> : null}
      <rect fill="currentColor" height="64" rx="14" width="64" />
      <path className="brand-mark__arrow brand-mark__arrow--blue" d="M14 17 29 32 14 47" />
      <path className="brand-mark__arrow brand-mark__arrow--green" d="m28 12 16 20-16 20" />
      <path className="brand-mark__arrow brand-mark__arrow--light" d="m43 18 9 14-9 14" />
    </svg>
  );
}
