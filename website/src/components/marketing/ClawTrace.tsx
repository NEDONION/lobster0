'use client';

import { motion } from 'motion/react';
import { useEffect, useRef, useState } from 'react';

import type { MarketingCopy } from '@/content/site';
import { useReducedMotionPreference } from '@/lib/motion';

import { hasSurfaceIcon, SurfaceIcon } from './SurfaceIcon';

interface ClawTraceProps {
  copy: MarketingCopy['trace'];
  surfaces: MarketingCopy['hero']['surfaces'];
}

const traceDuration = 2400;

export function ClawTrace({ copy, surfaces }: ClawTraceProps) {
  const traceRef = useRef<HTMLDivElement>(null);
  const reducedMotion = useReducedMotionPreference();
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
    <div className="claw-trace hero-network" ref={traceRef}>
      <svg aria-hidden="true" className="hero-network__route" viewBox="0 0 1200 360">
        <path d="M80 185 C220 70 350 305 515 176 S810 74 1120 184" />
        <path className="hero-network__route-pulse" d="M80 185 C220 70 350 305 515 176 S810 74 1120 184" />
      </svg>
      <span aria-hidden="true" className="hero-network__signal hero-network__signal--one" />
      <span aria-hidden="true" className="hero-network__signal hero-network__signal--two" />
      <span aria-hidden="true" className="hero-network__signal hero-network__signal--three" />
      <ul className="hero-network__surfaces" aria-label="Lobster0 surfaces">
        {surfaces.map((surface, index) => {
          const hasIcon = hasSurfaceIcon(surface.name);

          return (
            <li className="hero-surface-card" data-surface={index + 1} key={surface.name}>
              <span aria-hidden="true" data-brand={hasIcon}>
                {hasIcon ? <SurfaceIcon name={surface.name} /> : surface.name.slice(0, 2).toUpperCase()}
              </span>
              <strong>{surface.name}</strong>
              <small>{surface.role}</small>
              <i>{surface.note}</i>
            </li>
          );
        })}
      </ul>
      <div className="hero-network__caption">
        <strong>{copy.title}</strong>
      </div>
      <ol aria-label={copy.ariaLabel} className="hero-network__trace">
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
              <span aria-hidden="true" className="claw-trace__rail"><i /></span>
              <span className="claw-trace__event">{step.event}</span>
              <span className="claw-trace__state">{state === 'active' ? step.state : state}</span>
            </motion.li>
          );
        })}
      </ol>
    </div>
  );
}
