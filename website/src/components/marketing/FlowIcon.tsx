import type { FlowIcon as FlowIconId } from '@/content/site';

const paths: Record<FlowIconId, string> = {
  intent:
    '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4 4v-4H6.5A2.5 2.5 0 0 1 4 13.5v-8Z"/>',
  argv:
    '<path d="M9 4H6a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h3M15 4h3a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-3"/><line x1="10.5" y1="9" x2="10.5" y2="9.01"/><line x1="13.5" y1="12" x2="13.5" y2="12.01"/><line x1="10.5" y1="15" x2="10.5" y2="15.01"/>',
  gate: '<path d="M12 3.2 19 6.4v5.2c0 4.9-3 8.2-7 9.7-4-1.5-7-4.8-7-9.7V6.4l7-3.2Z"/>',
  run: '<rect x="3" y="4" width="18" height="16" rx="3.4"/><polyline points="7.4,10 10.8,12.5 7.4,15"/><line x1="12.6" y1="15" x2="16.6" y2="15"/>',
  result:
    '<circle cx="12" cy="12" r="9"/><polyline points="7.8,12.4 10.6,15.2 16.2,8.8"/>',
  program:
    '<path d="M7.5 3h6l4 4v14h-10.5V3Z"/><path d="M13.5 3v4h4"/><polyline points="9.3,13 7.3,15 9.3,17"/><polyline points="12.7,13 14.7,15 12.7,17"/>',
};

export function FlowIcon({ id }: { id: FlowIconId }) {
  return (
    <svg
      aria-hidden="true"
      dangerouslySetInnerHTML={{ __html: paths[id] }}
      fill="none"
      height="18"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.6"
      viewBox="0 0 24 24"
      width="18"
    />
  );
}
