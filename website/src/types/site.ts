import type content from '../data/site-content.json';
import type facts from '../data/project-facts.json';

export type Locale = keyof typeof content;
export type SiteContent = (typeof content)[Locale];
export type ProjectFacts = typeof facts;

export interface LocalizedRoute {
  home: '/' | '/en/';
  docs: '/docs/' | '/en/docs/';
  alternate: '/' | '/en/';
}
