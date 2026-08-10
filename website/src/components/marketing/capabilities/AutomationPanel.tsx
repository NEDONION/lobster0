'use client';

import { motion } from 'motion/react';
import { useEffect, useRef, useState } from 'react';

import { siteFacts, type CapabilityCopy, type Locale } from '@/content/site';
import { useReducedMotionPreference } from '@/lib/motion';

import { SurfaceIcon } from '../SurfaceIcon';

export function AutomationPanel({ copy, locale }: { copy: CapabilityCopy; locale: Locale }) {
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
    <article className="capability-panel capability-panel--automation">
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
        aria-label={zh ? 'Discord 里一次被拦截的定时任务' : 'A blocked scheduled task inside Discord'}
        className="chat-scene"
        ref={rootRef}
      >
        <div className="chat-scene__window">
          <div className="chat-scene__bar">
            <span aria-hidden="true" className="chat-scene__app-icon">
              <SurfaceIcon name="Discord" />
            </span>
            <span>lobster0 · {zh ? '社区入口' : 'community surface'}</span>
          </div>
          <div className="chat-scene__thread">
            <motion.p className="chat-bubble chat-bubble--agent" {...beat(0)}>
              {zh ? '定时任务 daily-report 已触发' : 'Scheduled task daily-report just fired'}
            </motion.p>
            <motion.div className="chat-card chat-card--pending" {...beat(1)}>
              <span className="chat-card__label">AUTOMATION_GATE · {zh ? '已拦截' : 'blocked'}</span>
              <code>{zh ? '自动化默认关闭，需要 Owner 显式启用' : 'automation is off by default — needs explicit owner opt-in'}</code>
              <div className="automation-toggle">
                <span className="automation-toggle__switch" data-on="false">
                  <i />
                </span>
                <strong>{siteFacts.status.automationDefault ? (zh ? '已开启' : 'ENABLED') : (zh ? '默认关闭' : 'DISABLED BY DEFAULT')}</strong>
              </div>
            </motion.div>
            <motion.div className="chat-card chat-card--done" {...beat(2)}>
              <span className="chat-card__label">{zh ? '一旦开启' : 'ONCE ENABLED'}</span>
              <div className="automation-flow">
                <span>{zh ? '所有者启用' : 'owner enables'}</span>
                <i aria-hidden="true">→</i>
                <span>{zh ? '策略门禁' : 'policy gate'}</span>
                <i aria-hidden="true">→</i>
                <span>{siteFacts.counts.automationCases} {zh ? '条版本化场景' : 'versioned cases'}</span>
              </div>
            </motion.div>
            <motion.p className="chat-scene__disclosure" {...beat(3)}>
              {zh ? 'Implementation PASS' : 'Implementation PASS'} <strong>≠</strong>{' '}
              {zh ? 'Live PASS' : 'Live PASS'}
            </motion.p>
          </div>
        </div>
      </div>
    </article>
  );
}
