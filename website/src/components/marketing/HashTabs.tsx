'use client';

import { motion } from 'motion/react';
import type { KeyboardEvent, ReactNode } from 'react';
import { useEffect, useState } from 'react';

import { useReducedMotionPreference } from '@/lib/motion';

export interface HashTabItem {
  id: string;
  label: string;
  panel: ReactNode;
}

interface HashTabsProps {
  ariaLabel: string;
  items: readonly HashTabItem[];
}

export function HashTabs({ ariaLabel, items }: HashTabsProps) {
  const firstId = items[0]?.id ?? '';
  const [selectedId, setSelectedId] = useState(firstId);
  const reducedMotion = useReducedMotionPreference();

  useEffect(() => {
    const syncFromHash = () => {
      const hashId = decodeURIComponent(window.location.hash.slice(1));
      setSelectedId(items.some((item) => item.id === hashId) ? hashId : firstId);
    };

    queueMicrotask(syncFromHash);
    window.addEventListener('hashchange', syncFromHash);
    return () => window.removeEventListener('hashchange', syncFromHash);
  }, [firstId, items]);

  if (!firstId) return null;
  const selectedItem = items.find((item) => item.id === selectedId) ?? items[0];
  if (!selectedItem) return null;

  const activate = (id: string, updateHash: boolean) => {
    setSelectedId(id);
    if (updateHash) history.replaceState(null, '', `#${encodeURIComponent(id)}`);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | null = null;
    if (event.key === 'ArrowRight') nextIndex = (index + 1) % items.length;
    if (event.key === 'ArrowLeft') nextIndex = (index - 1 + items.length) % items.length;
    if (event.key === 'Home') nextIndex = 0;
    if (event.key === 'End') nextIndex = items.length - 1;
    if (nextIndex === null) return;

    event.preventDefault();
    const nextItem = items[nextIndex];
    if (!nextItem) return;
    activate(nextItem.id, true);
    document.getElementById(`${nextItem.id}-tab`)?.focus();
  };

  return (
    <div className="hash-tabs">
      <div className="hash-tabs__list" role="tablist" aria-label={ariaLabel}>
        {items.map((item, index) => {
          const selected = item.id === selectedItem.id;
          return (
            <button
              aria-controls={`${item.id}-panel`}
              aria-selected={selected}
              id={`${item.id}-tab`}
              key={item.id}
              onClick={() => activate(item.id, true)}
              onKeyDown={(event) => handleKeyDown(event, index)}
              role="tab"
              tabIndex={selected ? 0 : -1}
              type="button"
            >
              <span aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
              {item.label}
            </button>
          );
        })}
      </div>
      <motion.div
        animate={{ opacity: 1, y: 0 }}
        aria-labelledby={`${selectedItem.id}-tab`}
        data-reduced-motion={reducedMotion ? 'true' : 'false'}
        id={`${selectedItem.id}-panel`}
        initial={reducedMotion ? false : { opacity: 0, y: 12 }}
        key={selectedItem.id}
        role="tabpanel"
        transition={{ duration: reducedMotion ? 0 : 0.28, ease: 'easeOut' }}
      >
        {selectedItem.panel}
      </motion.div>
    </div>
  );
}
