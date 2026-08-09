import { ImageResponse } from 'next/og';

export const alt = 'MiniClaw — Your local agent, ready to act.';
export const size = { height: 630, width: 1200 };
export const contentType = 'image/png';

function ArrowMark({ size: markSize }: { size: number }) {
  return (
    <svg height={markSize} viewBox="0 0 64 64" width={markSize}>
      <rect fill="#121722" height="64" rx="14" width="64" />
      <path d="M14 17 29 32 14 47" fill="none" stroke="#5B72FF" strokeLinecap="round" strokeWidth="6" />
      <path d="m28 12 16 20-16 20" fill="none" stroke="#70D6A8" strokeLinecap="round" strokeWidth="6" />
      <path d="m43 18 9 14-9 14" fill="none" stroke="#EEF2F7" strokeLinecap="round" strokeWidth="6" />
    </svg>
  );
}

const surfaces = [
  { color: '#4267F5', initials: 'TU', name: 'TUI', role: 'Local control', x: 56, y: 205 },
  { color: '#F16C56', initials: 'FE', name: 'Feishu', role: 'Work surface', x: 42, y: 350 },
  { color: '#2CAF88', initials: 'TE', name: 'Telegram', role: 'Mobile surface', x: 964, y: 220 },
  { color: '#7567D9', initials: 'DI', name: 'Discord', role: 'Community', x: 986, y: 365 },
] as const;

const metrics = [
  ['01', 'Python core', '#4267F5'],
  ['04', 'surfaces', '#F16C56'],
  ['18', 'built-in tools', '#2CAF88'],
  ['33', 'Channel cases', '#E4A93D'],
  ['15', 'Automation cases', '#7567D9'],
] as const;

export default function OpenGraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          background: '#F4F7FB',
          color: '#121722',
          display: 'flex',
          flexDirection: 'column',
          fontFamily: 'sans-serif',
          height: '100%',
          overflow: 'hidden',
          padding: '38px 54px 34px',
          position: 'relative',
          width: '100%',
        }}
      >
        <svg height="300" style={{ left: 80, position: 'absolute', top: 158 }} viewBox="0 0 1040 300" width="1040">
          <path d="M20 150 C180 38 315 254 485 145 S760 52 1020 150" fill="none" stroke="#4267F5" strokeDasharray="3 10" strokeOpacity="0.28" strokeWidth="2" />
          <circle cx="190" cy="102" fill="#F16C56" r="6" stroke="white" strokeWidth="3" />
          <circle cx="742" cy="83" fill="#2CAF88" r="6" stroke="white" strokeWidth="3" />
          <circle cx="888" cy="119" fill="#E4A93D" r="6" stroke="white" strokeWidth="3" />
        </svg>

        <div style={{ alignItems: 'center', display: 'flex', justifyContent: 'space-between' }}>
          <div style={{ alignItems: 'center', display: 'flex', fontSize: 24, fontWeight: 750, gap: 12 }}>
            <ArrowMark size={42} />
            <span>MiniClaw</span>
          </div>
          <div style={{ color: '#4267F5', display: 'flex', fontSize: 14, fontWeight: 700, letterSpacing: 1.5 }}>
            LOCAL-FIRST · OPEN SOURCE · INSPECTABLE
          </div>
        </div>

        {surfaces.map((surface) => (
          <div
            key={surface.name}
            style={{
              alignItems: 'center',
              background: 'rgba(255,255,255,.96)',
              border: '1px solid rgba(18,23,34,.09)',
              borderRadius: 16,
              display: 'flex',
              gap: 11,
              left: surface.x,
              padding: '11px 13px',
              position: 'absolute',
              top: surface.y,
              width: 158,
            }}
          >
            <div
              style={{
                alignItems: 'center',
                background: `${surface.color}18`,
                borderRadius: 10,
                color: surface.color,
                display: 'flex',
                fontFamily: 'monospace',
                fontSize: 12,
                height: 38,
                justifyContent: 'center',
                width: 38,
              }}
            >
              {surface.initials}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              <span style={{ display: 'flex', fontSize: 14, fontWeight: 700 }}>{surface.name}</span>
              <span style={{ color: '#647087', display: 'flex', fontSize: 11 }}>{surface.role}</span>
            </div>
          </div>
        ))}

        <div style={{ alignItems: 'center', display: 'flex', flex: 1, flexDirection: 'column', justifyContent: 'center', paddingBottom: 74 }}>
          <ArrowMark size={72} />
          <span style={{ color: '#4267F5', display: 'flex', fontSize: 14, fontWeight: 750, letterSpacing: 1.1, marginTop: 16 }}>
            ONE LOCAL CORE · EXPLICIT BOUNDARIES
          </span>
          <div style={{ alignItems: 'center', display: 'flex', flexDirection: 'column', fontSize: 52, fontWeight: 760, letterSpacing: -2.7, lineHeight: 1.02, marginTop: 13 }}>
            <span style={{ display: 'flex' }}>Your local agent,</span>
            <span style={{ display: 'flex' }}>ready to act.</span>
          </div>
          <span style={{ color: '#647087', display: 'flex', fontSize: 18, marginTop: 15 }}>
            Understand intent. Check policy. Approve exact actions. Deliver the result.
          </span>
        </div>

        <div
          style={{
            background: 'rgba(255,255,255,.97)',
            border: '1px solid rgba(18,23,34,.09)',
            borderRadius: 18,
            bottom: 34,
            display: 'flex',
            left: 54,
            overflow: 'hidden',
            position: 'absolute',
            right: 54,
          }}
        >
          {metrics.map(([value, label, color]) => (
            <div key={label} style={{ borderRight: '1px solid #E2E7EF', display: 'flex', flex: 1, flexDirection: 'column', gap: 4, padding: '14px 18px', position: 'relative' }}>
              <span style={{ background: color, display: 'flex', height: 3, left: 0, position: 'absolute', right: 0, top: 0 }} />
              <strong style={{ display: 'flex', fontFamily: 'monospace', fontSize: 22 }}>{value}</strong>
              <span style={{ color: '#647087', display: 'flex', fontSize: 11 }}>{label}</span>
            </div>
          ))}
          <div style={{ alignItems: 'center', color: '#7D621E', display: 'flex', fontSize: 11, lineHeight: 1.35, padding: '14px 18px', width: 230 }}>
            Implementation PASS is not Live PASS.
          </div>
        </div>
      </div>
    ),
    size,
  );
}
