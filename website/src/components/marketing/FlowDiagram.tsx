'use client';

import { motion } from 'motion/react';
import { useEffect, useRef, useState } from 'react';

import type { FlowStepCopy } from '@/content/site';
import { useReducedMotionPreference } from '@/lib/motion';

import { FlowIcon } from './FlowIcon';

export function FlowDiagram({ ariaLabel, steps }: { ariaLabel: string; steps: readonly FlowStepCopy[] }) {
  const reducedMotion = useReducedMotionPreference();
  const rootRef = useRef<HTMLDivElement>(null);
  const [started, setStarted] = useState(false);

  useEffect(() => {
    if (reducedMotion || started) return;
    const node = rootRef.current;
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
      { threshold: [0, 0.4] },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [reducedMotion, started]);

  const play = started || reducedMotion;

  return (
    <div className="flow-diagram" data-reduced-motion={reducedMotion ? 'true' : 'false'} ref={rootRef}>
      <ol aria-label={ariaLabel} className="flow-diagram__nodes">
        {steps.map((step, index) => (
          <motion.li
            animate={play ? { opacity: 1, y: 0 } : undefined}
            data-state={step.state}
            initial={reducedMotion ? false : { opacity: 0, y: 10 }}
            key={step.label}
            transition={{ delay: reducedMotion ? 0 : index * 0.14, duration: 0.42, ease: 'easeOut' }}
          >
            <span aria-hidden="true" className="flow-diagram__icon">
              <FlowIcon id={step.icon} />
            </span>
            <strong>{step.label}</strong>
            <code>{step.detail}</code>
          </motion.li>
        ))}
      </ol>
    </div>
  );
}
