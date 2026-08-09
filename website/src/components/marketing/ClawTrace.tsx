'use client';

import { motion } from 'motion/react';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';

import type { MarketingCopy } from '@/content/site';

interface ClawTraceProps {
  copy: MarketingCopy['trace'];
}

const traceDuration = 2400;
const reducedMotionQuery = '(prefers-reduced-motion: reduce)';

function subscribeToReducedMotion(onChange: () => void) {
  if (typeof window.matchMedia !== 'function') return () => undefined;
  const media = window.matchMedia(reducedMotionQuery);
  media.addEventListener('change', onChange);
  return () => media.removeEventListener('change', onChange);
}

function getReducedMotionPreference() {
  return typeof window.matchMedia === 'function' && window.matchMedia(reducedMotionQuery).matches;
}

export function ClawTrace({ copy }: ClawTraceProps) {
  const traceRef = useRef<HTMLDivElement>(null);
  const reducedMotion = useSyncExternalStore(
    subscribeToReducedMotion,
    getReducedMotionPreference,
    () => false,
  );
  const [started, setStarted] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const lastIndex = copy.steps.length - 1;
  const visibleIndex = reducedMotion ? lastIndex : activeIndex;

  useEffect(() => {
    if (reducedMotion || started) return;
    const node = traceRef.current;
    if (!node) return;

    if (!('IntersectionObserver' in window)) {
      const animationFrame = requestAnimationFrame(() => setStarted(true));
      return () => cancelAnimationFrame(animationFrame);
    }

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (!entry?.isIntersecting) return;
        setStarted(true);
        observer.disconnect();
      },
      { threshold: 0.35 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [reducedMotion, started]);

  useEffect(() => {
    if (!started || reducedMotion || lastIndex < 1) return;

    let animationFrame = 0;
    let elapsed = 0;
    let previousTime = performance.now();
    const stepDuration = traceDuration / lastIndex;

    const resetFrameClock = () => {
      previousTime = performance.now();
    };

    const advance = (time: number) => {
      if (!document.hidden) {
        elapsed += time - previousTime;
        const nextIndex = Math.min(lastIndex, Math.floor(elapsed / stepDuration));
        setActiveIndex((current) => (current === nextIndex ? current : nextIndex));
        if (nextIndex === lastIndex) return;
      }
      previousTime = time;
      animationFrame = requestAnimationFrame(advance);
    };

    document.addEventListener('visibilitychange', resetFrameClock);
    animationFrame = requestAnimationFrame(advance);
    return () => {
      cancelAnimationFrame(animationFrame);
      document.removeEventListener('visibilitychange', resetFrameClock);
    };
  }, [lastIndex, reducedMotion, started]);

  return (
    <div className="claw-trace" ref={traceRef}>
      <div className="claw-trace__topline">
        <div>
          <span>{copy.eyebrow}</span>
          <strong>{copy.title}</strong>
        </div>
        <span className="claw-trace__status">
          <i aria-hidden="true" /> runtime.connected
        </span>
      </div>
      <p className="claw-trace__description">{copy.description}</p>
      <ol aria-label="Claw Trace">
        {copy.steps.map((step, index) => {
          const state = index < visibleIndex ? 'complete' : index === visibleIndex ? 'active' : 'pending';

          return (
            <motion.li
              animate={{ opacity: state === 'pending' ? 0.45 : 1, x: state === 'active' ? 4 : 0 }}
              aria-current={state === 'active' ? 'step' : undefined}
              data-state={state}
              initial={false}
              key={step.event}
              transition={{ duration: reducedMotion ? 0 : 0.22, ease: 'easeOut' }}
            >
              <span className="claw-trace__rail" aria-hidden="true">
                <i />
              </span>
              <span className="claw-trace__index">{String(index + 1).padStart(2, '0')}</span>
              <span className="claw-trace__event">{step.event}</span>
              <span className="claw-trace__detail">{step.detail}</span>
              <span className="claw-trace__state">{state === 'active' ? step.state : state}</span>
            </motion.li>
          );
        })}
      </ol>
    </div>
  );
}
