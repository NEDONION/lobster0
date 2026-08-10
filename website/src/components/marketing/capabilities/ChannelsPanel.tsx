'use client';

import { motion } from 'motion/react';

import { marketingCopy, siteFacts, type CapabilityCopy, type Locale } from '@/content/site';
import { useReducedMotionPreference } from '@/lib/motion';

import { SurfaceIcon } from '../SurfaceIcon';

const edgeX = [12, 37.3, 62.6, 88];

export function ChannelsPanel({ copy, locale }: { copy: CapabilityCopy; locale: Locale }) {
  const ui = marketingCopy[locale].ui;
  const reducedMotion = useReducedMotionPreference();
  const surfaces = marketingCopy[locale].hero.surfaces;

  return (
    <article className="capability-panel capability-panel--channels">
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
        aria-label={ui.channelsAria}
        className="channels-map"
        data-reduced-motion={reducedMotion ? 'true' : 'false'}
      >
        <motion.div
          animate={{ opacity: 1, scale: 1 }}
          className="channels-map__core"
          initial={reducedMotion ? false : { opacity: 0, scale: 0.85 }}
          transition={{ duration: 0.4, ease: 'easeOut' }}
        >
          <span>{ui.sharedCoreShort}</span>
          <strong>AgentRuntime</strong>
          <small>{ui.coreStackShort}</small>
        </motion.div>
        <div aria-hidden="true" className="channels-map__wires">
          <span className="channels-map__wire channels-map__wire--trunk" />
          <span className="channels-map__wire channels-map__wire--bus" />
          {edgeX.map((x) => (
            <span className="channels-map__wire channels-map__wire--drop" key={x} style={{ left: `${x}%` }} />
          ))}
        </div>
        <ul className="channels-map__edges">
          {siteFacts.surfaces.map((surfaceId, index) => {
            const surface = surfaces.find((item) => item.name === surfaceId) ?? surfaces[index];
            return (
              <motion.li
                animate={{ opacity: 1, y: 0 }}
                initial={reducedMotion ? false : { opacity: 0, y: 12 }}
                key={surfaceId}
                style={{ left: `${edgeX[index]}%`, x: '-50%' }}
                transition={{ delay: reducedMotion ? 0 : 0.25 + index * 0.1, duration: 0.4, ease: 'easeOut' }}
              >
                <span aria-hidden="true" className="channels-map__edge-icon" data-brand="true">
                  <SurfaceIcon name={surface?.name ?? surfaceId} />
                </span>
                <strong>{surface?.name ?? surfaceId}</strong>
                <small>{ui.edgeSummary}</small>
              </motion.li>
            );
          })}
        </ul>
      </div>
    </article>
  );
}
