'use client';

import { motion } from 'motion/react';
import { useEffect, useRef, useState } from 'react';

import type { CapabilityCopy, Locale } from '@/content/site';
import { useReducedMotionPreference } from '@/lib/motion';

export function MemoryPanel({ copy, locale }: { copy: CapabilityCopy; locale: Locale }) {
  const zh = locale === 'zh-CN';
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
      { threshold: 0.4 },
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
        aria-label={zh ? 'Markdown 事实源与 SQLite 投影' : 'Markdown truth and SQLite projection'}
        className="memory-scene"
        ref={rootRef}
      >
        <div className="memory-scene__bar">
          <span>~/.lobster0/memory</span>
          <em>{zh ? '所有者边界 · 本地工作区' : 'owner boundary · local workspace'}</em>
        </div>
        <div className="memory-scene__panes">
          <motion.div
            animate={play ? { opacity: 1, x: 0 } : undefined}
            className="memory-pane"
            initial={reducedMotion ? false : { opacity: 0, x: -10 }}
            transition={{ duration: 0.4, ease: 'easeOut' }}
          >
            <div className="memory-pane__label">facts.md · {zh ? '事实源' : 'TRUTH'}</div>
            <pre>
              <span className="tok-h"># deploy</span>
              {'\n'}
              <span className="tok-b">- owner:</span> ned
              {'\n'}
              <span className="tok-b">- target:</span> vercel
              {'\n'}
              <span className="tok-b">- domain:</span> lobster0.jchu.tech
              {'\n'}
              <span className="tok-b">- released:</span> 2026-08-10
            </pre>
          </motion.div>
          <motion.div
            animate={play ? { opacity: 1 } : undefined}
            className="memory-scene__link"
            initial={reducedMotion ? false : { opacity: 0 }}
            transition={{ delay: reducedMotion ? 0 : 0.35, duration: 0.3 }}
          >
            <span>{zh ? '投影' : 'projection'}</span>
            <i aria-hidden="true" />
          </motion.div>
          <motion.div
            animate={play ? { opacity: 1, x: 0 } : undefined}
            className="memory-pane"
            initial={reducedMotion ? false : { opacity: 0, x: 10 }}
            transition={{ delay: reducedMotion ? 0 : 0.15, duration: 0.4, ease: 'easeOut' }}
          >
            <div className="memory-pane__label">SQLite · {zh ? '控制面' : 'CONTROL PLANE'}</div>
            <pre>
              <span className="tok-k">SELECT</span> key, value <span className="tok-k">FROM</span> facts;
              {'\n\n'}
              key      value
              {'\n'}
              -------- -------------------
              {'\n'}
              owner    ned
              {'\n'}
              target   vercel
              {'\n'}
              domain   lobster0.jchu.tech
            </pre>
          </motion.div>
        </div>
      </div>
    </article>
  );
}
