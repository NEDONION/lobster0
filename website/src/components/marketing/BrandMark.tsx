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
      viewBox="0 0 240 240"
      width={size}
      xmlns="http://www.w3.org/2000/svg"
    >
      {title ? <title>{title}</title> : null}
      <defs>
        <mask id="lobster-claw-notch">
          <rect fill="#fff" height="240" width="240" x="0" y="0" />
          <path d="M22,102 L46,110 L22,122 Z" fill="#000" />
          <path d="M218,102 L194,110 L218,122 Z" fill="#000" />
        </mask>
      </defs>

      <g id="lobster-half">
        <path
          d="M110,96 Q92,64 76,44"
          fill="none"
          stroke="#c94827"
          strokeLinecap="round"
          strokeWidth="5"
        />
        <path d="M98,116 L74,104" stroke="#e8492c" strokeLinecap="round" strokeWidth="9" />
        <path
          d="M80,112 C78,96 66,84 50,84 C34,84 24,96 26,112 C28,128 42,138 58,136 C70,134 78,126 80,112 Z"
          fill="#e8492c"
          mask="url(#lobster-claw-notch)"
        />
      </g>
      <use href="#lobster-half" transform="scale(-1,1) translate(-240,0)" />

      <ellipse cx="120" cy="120" fill="#f0532f" rx="34" ry="30" />
      <circle cx="108" cy="112" fill="#1a1d24" r="6" />
      <circle cx="132" cy="112" fill="#1a1d24" r="6" />
      <circle cx="110" cy="110" fill="#fff" r="1.8" />
      <circle cx="134" cy="110" fill="#fff" r="1.8" />

      <ellipse cx="120" cy="156" fill="#e8492c" rx="26" ry="20" />
      <ellipse cx="120" cy="184" fill="#f0532f" rx="21" ry="17" />
      <ellipse cx="120" cy="207" fill="#e8492c" rx="16" ry="14" />
      <path d="M120,214 C112,214 100,222 96,234 C104,238 112,234 118,224 Z" fill="#f6b73c" />
      <path d="M120,214 C128,214 140,222 144,234 C136,238 128,234 122,224 Z" fill="#f6b73c" />
      <path
        d="M120,216 C114,220 112,230 116,240 C120,242 124,240 126,232 C127,224 124,218 120,216 Z"
        fill="#e8a52f"
      />

      <path
        d="M96,148 L82,160 M100,168 L86,180 M140,148 L154,160 M140,168 L154,180"
        fill="none"
        stroke="#c94827"
        strokeLinecap="round"
        strokeWidth="4"
      />
    </svg>
  );
}
