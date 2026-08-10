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
          <path d="M34,132 L52,150 L30,152 Z" fill="#000" />
          <path d="M206,132 L188,150 L210,152 Z" fill="#000" />
        </mask>
      </defs>

      {/* head */}
      <ellipse cx="120" cy="104" fill="#f0532f" rx="30" ry="26" />
      <path
        d="M108,84 Q92,56 78,40"
        fill="none"
        stroke="#c94827"
        strokeLinecap="round"
        strokeWidth="5"
      />
      <path
        d="M132,84 Q148,56 162,40"
        fill="none"
        stroke="#c94827"
        strokeLinecap="round"
        strokeWidth="5"
      />
      <circle cx="110" cy="98" fill="#1a1d24" r="5.5" />
      <circle cx="130" cy="98" fill="#1a1d24" r="5.5" />
      <circle cx="112" cy="96" fill="#fff" r="1.7" />
      <circle cx="132" cy="96" fill="#fff" r="1.7" />

      {/* thorax + tail */}
      <ellipse cx="120" cy="146" fill="#e8492c" rx="32" ry="28" />
      <ellipse cx="120" cy="180" fill="#f0532f" rx="25" ry="19" />
      <ellipse cx="120" cy="204" fill="#e8492c" rx="18" ry="15" />
      <path d="M120,212 C112,212 100,220 96,232 C104,236 112,232 118,222 Z" fill="#f6b73c" />
      <path d="M120,212 C128,212 140,220 144,232 C136,236 128,232 122,222 Z" fill="#f6b73c" />
      <path
        d="M120,214 C114,218 112,228 116,238 C120,240 124,238 126,230 C127,222 124,216 120,214 Z"
        fill="#e8a52f"
      />

      {/* walking legs */}
      <path
        d="M92,158 L76,170 M96,176 L82,188 M148,158 L164,170 M144,176 L158,188"
        fill="none"
        stroke="#c94827"
        strokeLinecap="round"
        strokeWidth="4"
      />

      {/* claws: arms leave the thorax and reach forward, clear of the head */}
      <path d="M96,140 L70,150" stroke="#e8492c" strokeLinecap="round" strokeWidth="10" />
      <path
        d="M72,152 C70,138 60,128 46,128 C32,128 24,138 26,152 C28,166 40,174 54,172 C64,170 71,164 72,152 Z"
        fill="#e8492c"
        mask="url(#lobster-claw-notch)"
      />
      <path d="M144,140 L170,150" stroke="#e8492c" strokeLinecap="round" strokeWidth="10" />
      <path
        d="M168,152 C170,138 180,128 194,128 C208,128 216,138 214,152 C212,166 200,174 186,172 C176,170 169,164 168,152 Z"
        fill="#e8492c"
        mask="url(#lobster-claw-notch)"
      />
    </svg>
  );
}
