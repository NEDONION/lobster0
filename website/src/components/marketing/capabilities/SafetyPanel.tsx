'use client';

import { motion } from 'motion/react';
import { useEffect, useRef, useState } from 'react';

import { marketingCopy, siteFacts, type CapabilityCopy, type Locale } from '@/content/site';
import { useReducedMotionPreference } from '@/lib/motion';

import { SurfaceIcon } from '../SurfaceIcon';

export function SafetyPanel({ copy, locale }: { copy: CapabilityCopy; locale: Locale }) {
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
  const beat = (index: number) => ({
    initial: reducedMotion ? false : { opacity: 0, y: 8 },
    animate: play ? { opacity: 1, y: 0 } : undefined,
    transition: { delay: reducedMotion ? 0 : 0.3 + index * 0.35, duration: 0.36, ease: 'easeOut' as const },
  });

  return (
    <article className="capability-panel capability-panel--safety">
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
        aria-label={ui.safetyAria}
        className="chat-scene"
        ref={rootRef}
      >
        <div className="chat-scene__window">
          <div className="chat-scene__bar">
            <span aria-hidden="true" className="chat-scene__app-icon">
              <SurfaceIcon name="飞书" />
            </span>
            <span>lobster0 · {ui.workSurface}</span>
          </div>
          <div className="chat-scene__thread">
            <motion.p className="chat-bubble chat-bubble--user" {...beat(0)}>
              {ui.safetyAsk}
            </motion.p>
            <motion.p className="chat-bubble chat-bubble--agent" {...beat(1)}>
              {ui.safetyReply}
            </motion.p>
            <motion.div className="chat-card chat-card--pending" {...beat(2)}>
              <span className="chat-card__label">POLICY_CHECK · {ui.awaitingConfirm}</span>
              <code>rm -rf /tmp/lobster0-cache-2026</code>
              <div className="chat-modes">
                {siteFacts.permissionModes.map((mode, index) => (
                  <button data-picked={index === 0 ? 'true' : undefined} key={mode} type="button">
                    {mode}
                  </button>
                ))}
              </div>
            </motion.div>
            <motion.div className="chat-card chat-card--done" {...beat(3)}>
              <span className="chat-card__label">RESULT_DELIVERED</span>
              <div className="chat-checks">
                <span>✓ {ui.checkWorkspace}</span>
                <span>✓ {ui.checkCommand}</span>
                <span>✓ {ui.checkNetwork}</span>
                <span>✓ {ui.checkSecret}</span>
              </div>
            </motion.div>
          </div>
        </div>
      </div>
    </article>
  );
}
