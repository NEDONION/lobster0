'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';

import type { Locale } from '@/content/site';
import { localeNames, localeShortNames, localizedPath, locales } from '@/lib/i18n';

function GlobeIcon() {
  return (
    <svg aria-hidden="true" fill="none" height="18" viewBox="0 0 24 24" width="18">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M3 12h18M12 3c2.4 2.4 3.6 5.6 3.6 9s-1.2 6.6-3.6 9c-2.4-2.4-3.6-5.6-3.6-9S9.6 5.4 12 3Z"
        stroke="currentColor"
        strokeWidth="1.6"
      />
    </svg>
  );
}

export function LanguageSwitcher({ locale, label }: { locale: Locale; label: string }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  return (
    <div className="language-switch" ref={rootRef}>
      <button
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={label}
        className="language-switch__toggle"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <GlobeIcon />
        <span>{localeShortNames[locale]}</span>
      </button>
      {open ? (
        <ul className="language-switch__menu" role="listbox">
          {locales.map((item) => (
            <li key={item}>
              <Link
                aria-current={item === locale ? 'true' : undefined}
                href={localizedPath(item, '/')}
                onClick={() => setOpen(false)}
                role="option"
              >
                {localeNames[item]}
              </Link>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
