'use client';

import { motion } from 'motion/react';
import { useEffect, useRef, useState } from 'react';

import { marketingCopy, type CapabilityCopy, type Locale } from '@/content/site';
import { useReducedMotionPreference } from '@/lib/motion';

export function MemoryPanel({ copy, locale }: { copy: CapabilityCopy; locale: Locale }) {
  const ui = marketingCopy[locale].ui;
  const reducedMotion = useReducedMotionPreference();
  const rootRef = useRef<HTMLDivElement>(null);
  const [started, setStarted] = useState(false);

  useEffect(() => {
    if (reducedMotion || started) return;
    const node = rootRef.current;
    if (!node) return;
    if (!('IntersectionObserver' in window)) {
      const frame = requestAnimationFrame(() => setStarted(true));
      return () => cancelAnimationFrame(frame);
    }
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (!entry?.isIntersecting) return;
        setStarted(true);
        observer.disconnect();
      },
      { threshold: [0, 0.4] },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [reducedMotion, started]);

  const play = started || reducedMotion;

  return (
    <article className="capability-panel capability-panel--memory">
      <div className="capability-panel__copy">
        <span>{copy.eyebrow}</span>
        <h3>{copy.title}</h3>
        <p>{copy.summary}</p>
        <ul className="capability-facts">
          {copy.facts.map((fact) => (
            <li key={fact}>{fact}</li>
          ))}
        </ul>
      </div>
      <div
        aria-label={ui.memoryAria}
        className="memory-scene"
        ref={rootRef}
      >
        <div className="memory-scene__bar">
          <span>~/.lobster0/memory</span>
          <em>{ui.ownerBoundary}</em>
        </div>
        <div className="memory-scene__panes">
          <motion.div
            animate={play ? { opacity: 1, x: 0 } : undefined}
            className="memory-pane"
            initial={reducedMotion ? false : { opacity: 0, x: -10 }}
            transition={{ duration: 0.4, ease: 'easeOut' }}
          >
            <div className="memory-pane__label">
              {ui.longTerm}
            </div>
            <ul className="memory-list">
              <li>
                <b>{ui.memoryName}</b>
                <span>{ui.memoryNameValue}</span>
              </li>
              <li>
                <b>{ui.memoryLang}</b>
                <span>{ui.memoryLangValue}</span>
              </li>
              <li>
                <b>{ui.memoryHabit}</b>
                <span>{ui.memoryHabitValue}</span>
              </li>
              <li>
                <b>{ui.memoryProject}</b>
                <span>{ui.memoryProjectValue}</span>
              </li>
            </ul>
          </motion.div>
          <motion.div
            animate={play ? { opacity: 1 } : undefined}
            className="memory-scene__link"
            initial={reducedMotion ? false : { opacity: 0 }}
            transition={{ delay: reducedMotion ? 0 : 0.35, duration: 0.3 }}
          >
            <span>{ui.promote}</span>
            <i aria-hidden="true" />
          </motion.div>
          <motion.div
            animate={play ? { opacity: 1, x: 0 } : undefined}
            className="memory-pane"
            initial={reducedMotion ? false : { opacity: 0, x: 10 }}
            transition={{ delay: reducedMotion ? 0 : 0.15, duration: 0.4, ease: 'easeOut' }}
          >
            <div className="memory-pane__label memory-pane__label--short">
              {ui.shortTerm}
            </div>
            <ul className="memory-list memory-list--short">
              <li>
                <b>{ui.memoryDoing}</b>
                <span>{ui.memoryDoingValue}</span>
              </li>
              <li>
                <b>{ui.memoryJustSaid}</b>
                <span>{ui.memoryJustSaidValue}</span>
              </li>
              <li>
                <b>{ui.memoryOnEnd}</b>
                <span>{ui.memoryOnEndValue}</span>
              </li>
            </ul>
          </motion.div>
        </div>
      </div>
    </article>
  );
}
