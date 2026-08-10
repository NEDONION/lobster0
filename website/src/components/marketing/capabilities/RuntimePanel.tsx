'use client';

import { motion } from 'motion/react';
import { useEffect, useRef, useState } from 'react';

import { marketingCopy, type CapabilityCopy, type Locale } from '@/content/site';
import { useReducedMotionPreference } from '@/lib/motion';

export function RuntimePanel({ copy, locale }: { copy: CapabilityCopy; locale: Locale }) {
  const steps = marketingCopy[locale].trace.steps;
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
    <article className="capability-panel capability-panel--runtime">
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
      <div className="runtime-scene" ref={rootRef}>
        <div className="runtime-scene__bar">
          <span className="runtime-scene__dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <span>lobster0 trace --follow</span>
        </div>
        <ol className="runtime-map" aria-label={locale === 'zh-CN' ? 'Lobster0 运行路径' : 'Lobster0 runtime path'}>
          {steps.map((step, index) => (
            <motion.li
              animate={play ? { opacity: 1, x: 0 } : undefined}
              data-state={step.state}
              initial={reducedMotion ? false : { opacity: 0, x: -8 }}
              key={step.event}
              transition={{ delay: reducedMotion ? 0 : index * 0.16, duration: 0.32, ease: 'easeOut' }}
            >
              <span className="runtime-map__index">{String(index + 1).padStart(2, '0')}</span>
              <strong className="runtime-map__event">{step.event}</strong>
              <em className="runtime-map__state">{step.state}</em>
              <small className="runtime-map__detail">{step.detail}</small>
            </motion.li>
          ))}
        </ol>
      </div>
    </article>
  );
}
