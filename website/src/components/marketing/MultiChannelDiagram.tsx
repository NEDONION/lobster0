'use client';

import { motion } from 'motion/react';
import { useEffect, useRef, useState } from 'react';

import { marketingCopy, siteFacts, type Locale, type WorkflowCopy } from '@/content/site';
import { useReducedMotionPreference } from '@/lib/motion';

import { SurfaceIcon } from './SurfaceIcon';

export function MultiChannelDiagram({ locale, workflow }: { locale: Locale; workflow: WorkflowCopy }) {
  const ui = marketingCopy[locale].ui;
  const reducedMotion = useReducedMotionPreference();
  const rootRef = useRef<HTMLDivElement>(null);
  const [started, setStarted] = useState(false);
  const surfaces = marketingCopy[locale].hero.surfaces;

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
    <article className="workflow-panel workflow-panel--channels">
      <div className="workflow-panel__copy">
        <span>03 / {ui.boundaryMoment}</span>
        <h3>{workflow.title}</h3>
        <p>{workflow.summary}</p>
      </div>
      <div
        aria-label={ui.isolationAria}
        className="multi-channel-map"
        ref={rootRef}
      >
        <motion.div
          animate={{ opacity: 1, scale: 1 }}
          className="multi-channel-map__runtime"
          initial={reducedMotion ? false : { opacity: 0, scale: 0.85 }}
          transition={{ duration: 0.4, ease: 'easeOut' }}
        >
          <span>{ui.sharedCore}</span>
          <strong>AgentRuntime</strong>
          <small>{ui.coreStack}</small>
        </motion.div>
        <div className="multi-channel-map__surfaces">
          {siteFacts.surfaces.map((surfaceId, index) => {
            const surface = surfaces.find((item) => item.name === surfaceId) ?? surfaces[index];
            return (
              <motion.div
                animate={play ? { opacity: 1, y: 0 } : undefined}
                initial={reducedMotion ? false : { opacity: 0, y: 10 }}
                key={surfaceId}
                transition={{ delay: reducedMotion ? 0 : 0.2 + index * 0.1, duration: 0.4, ease: 'easeOut' }}
              >
                <header>
                  <span aria-hidden="true" className="multi-channel-map__icon" data-brand="true">
                    <SurfaceIcon name={surface?.name ?? surfaceId} />
                  </span>
                  <strong>{surface?.name ?? surfaceId}</strong>
                  <i aria-label={ui.healthy} />
                </header>
                <dl>
                  <div><dt>{ui.transport}</dt><dd>{ui.isolated}</dd></div>
                  <div><dt>{ui.delivery}</dt><dd>{ui.isolated}</dd></div>
                  <div><dt>{ui.queue}</dt><dd>{ui.isolated}</dd></div>
                  <div><dt>{ui.failure}</dt><dd>{ui.contained}</dd></div>
                </dl>
              </motion.div>
            );
          })}
        </div>
      </div>
    </article>
  );
}
