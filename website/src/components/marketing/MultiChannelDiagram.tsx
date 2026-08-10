'use client';

import { motion } from 'motion/react';
import { useEffect, useRef, useState } from 'react';

import { marketingCopy, siteFacts, type Locale, type WorkflowCopy } from '@/content/site';
import { useReducedMotionPreference } from '@/lib/motion';

import { SurfaceIcon } from './SurfaceIcon';

export function MultiChannelDiagram({ locale, workflow }: { locale: Locale; workflow: WorkflowCopy }) {
  const zh = locale === 'zh-CN';
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
      { threshold: 0.4 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [reducedMotion, started]);

  const play = started || reducedMotion;

  return (
    <article className="workflow-panel workflow-panel--channels">
      <div className="workflow-panel__copy">
        <span>03 / {zh ? '共享核心 · 隔离入口' : 'SHARED CORE · ISOLATED EDGES'}</span>
        <h3>{workflow.title}</h3>
        <p>{workflow.summary}</p>
        <dl>
          <div><dt>{zh ? '运行时' : 'RUNTIME'}</dt><dd>01 {zh ? '共享' : 'shared'}</dd></div>
          <div><dt>{zh ? '入口' : 'SURFACES'}</dt><dd>{String(siteFacts.counts.surfaces).padStart(2, '0')}</dd></div>
          <div><dt>{zh ? '场景' : 'CASES'}</dt><dd>{siteFacts.counts.channelCases}</dd></div>
        </dl>
      </div>
      <div
        aria-label={zh ? '共享 AgentRuntime 与隔离入口' : 'Shared AgentRuntime with isolated channel edges'}
        className="multi-channel-map"
        ref={rootRef}
      >
        <motion.div
          animate={{ opacity: 1, scale: 1 }}
          className="multi-channel-map__runtime"
          initial={reducedMotion ? false : { opacity: 0, scale: 0.85 }}
          transition={{ duration: 0.4, ease: 'easeOut' }}
        >
          <span>{zh ? '共享核心' : 'SHARED CORE'}</span>
          <strong>AgentRuntime</strong>
          <small>{zh ? 'Agent · 策略 · 工具 · 记忆' : 'Agent · Policy · Tools · Memory'}</small>
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
                  <i aria-label={zh ? '健康' : 'healthy'} />
                </header>
                <dl>
                  <div><dt>{zh ? '传输' : 'Transport'}</dt><dd>{zh ? '隔离' : 'isolated'}</dd></div>
                  <div><dt>{zh ? '交付' : 'Delivery'}</dt><dd>{zh ? '隔离' : 'isolated'}</dd></div>
                  <div><dt>{zh ? '队列' : 'queue'}</dt><dd>{zh ? '隔离' : 'isolated'}</dd></div>
                  <div><dt>{zh ? '故障' : 'failure'}</dt><dd>{zh ? '受控' : 'contained'}</dd></div>
                </dl>
              </motion.div>
            );
          })}
        </div>
      </div>
    </article>
  );
}
