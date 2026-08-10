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
            <div className="memory-pane__label">
              {zh ? '长期记忆 · 一直记得' : 'LONG-TERM · always remembered'}
            </div>
            <ul className="memory-list">
              <li>
                <b>{zh ? '称呼' : 'Name'}</b>
                <span>{zh ? '叫我 Ned' : 'Call me Ned'}</span>
              </li>
              <li>
                <b>{zh ? '语言' : 'Language'}</b>
                <span>{zh ? '默认用中文回复' : 'Reply in Chinese'}</span>
              </li>
              <li>
                <b>{zh ? '习惯' : 'Habit'}</b>
                <span>{zh ? '删文件前先问我' : 'Ask before deleting files'}</span>
              </li>
              <li>
                <b>{zh ? '项目' : 'Project'}</b>
                <span>{zh ? '主力仓库是 lobster0' : 'Main repo is lobster0'}</span>
              </li>
            </ul>
          </motion.div>
          <motion.div
            animate={play ? { opacity: 1 } : undefined}
            className="memory-scene__link"
            initial={reducedMotion ? false : { opacity: 0 }}
            transition={{ delay: reducedMotion ? 0 : 0.35, duration: 0.3 }}
          >
            <span>{zh ? '沉淀' : 'promote'}</span>
            <i aria-hidden="true" />
          </motion.div>
          <motion.div
            animate={play ? { opacity: 1, x: 0 } : undefined}
            className="memory-pane"
            initial={reducedMotion ? false : { opacity: 0, x: 10 }}
            transition={{ delay: reducedMotion ? 0 : 0.15, duration: 0.4, ease: 'easeOut' }}
          >
            <div className="memory-pane__label memory-pane__label--short">
              {zh ? '短期记忆 · 本次对话' : 'SHORT-TERM · this session'}
            </div>
            <ul className="memory-list memory-list--short">
              <li>
                <b>{zh ? '正在做' : 'Doing'}</b>
                <span>{zh ? '改官网 Logo' : 'Reworking the site logo'}</span>
              </li>
              <li>
                <b>{zh ? '刚提到' : 'Just said'}</b>
                <span>{zh ? '钳子要从胸口伸出' : 'Claws should come from the chest'}</span>
              </li>
              <li>
                <b>{zh ? '会话结束' : 'On session end'}</b>
                <span>{zh ? '重要的沉淀为长期' : 'Important bits promoted'}</span>
              </li>
            </ul>
          </motion.div>
        </div>
      </div>
    </article>
  );
}
