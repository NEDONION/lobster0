import { ImageResponse } from 'next/og';

export const alt = 'MiniClaw — Small by design. Ready to act.';
export const size = { height: 630, width: 1200 };
export const contentType = 'image/png';

const trace = [
  'MESSAGE_RECEIVED',
  'AGENT_PLANNING',
  'POLICY_CHECK',
  'APPROVAL',
  'TOOL_EXECUTION',
  'RESULT_DELIVERED',
];

export default function OpenGraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          background: '#f4f6fa',
          color: '#10131a',
          display: 'flex',
          flexDirection: 'column',
          fontFamily: 'sans-serif',
          height: '100%',
          padding: '54px 64px',
          position: 'relative',
          width: '100%',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 14, fontSize: 26, fontWeight: 700 }}>
            <span
              style={{
                alignItems: 'center',
                background: '#5b6cff',
                borderRadius: 12,
                color: 'white',
                display: 'flex',
                fontFamily: 'monospace',
                fontSize: 18,
                height: 46,
                justifyContent: 'center',
                width: 46,
              }}
            >
              M
            </span>
            MiniClaw
          </div>
          <span style={{ color: '#5b6cff', fontFamily: 'monospace', fontSize: 15, letterSpacing: 2 }}>
            LOCAL-FIRST / OPEN SOURCE
          </span>
        </div>

        <div style={{ display: 'flex', flex: 1, alignItems: 'center', gap: 54 }}>
          <div style={{ display: 'flex', flexDirection: 'column', flex: 1 }}>
            <div
              style={{
                display: 'flex',
                flexDirection: 'column',
                fontSize: 60,
                fontWeight: 730,
                letterSpacing: -3.5,
                lineHeight: 1.02,
              }}
            >
              <span>Small by design.</span>
              <span>Ready to act.</span>
            </div>
            <div
              style={{
                color: '#667085',
                display: 'flex',
                flexDirection: 'column',
                fontSize: 21,
                lineHeight: 1.45,
                marginTop: 24,
              }}
            >
              <span>One inspectable local Agent core.</span>
              <span>Explicit policy before every action.</span>
            </div>
          </div>

          <div
            style={{
              background: '#171b24',
              borderRadius: 24,
              color: '#f6f7fb',
              display: 'flex',
              flex: 1,
              flexDirection: 'column',
              padding: '28px 30px',
            }}
          >
            <div style={{ color: '#a7afbf', display: 'flex', fontFamily: 'monospace', fontSize: 14 }}>
              CLAW TRACE / 01
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', marginTop: 16 }}>
              {trace.map((event, index) => (
                <div
                  key={event}
                  style={{
                    alignItems: 'center',
                    borderTop: '1px solid rgba(255,255,255,.1)',
                    display: 'flex',
                    fontFamily: 'monospace',
                    fontSize: 13,
                    height: 47,
                  }}
                >
                  <span style={{ color: '#73f7c4', display: 'flex', marginRight: 12 }}>●</span>
                  <span style={{ color: '#7f899b', display: 'flex', marginRight: 16 }}>0{index + 1}</span>
                  <span style={{ display: 'flex', flex: 1 }}>{event}</span>
                  <span style={{ color: index === 5 ? '#73f7c4' : '#8d96a8', display: 'flex' }}>
                    {index === 5 ? 'SUCCEEDED' : 'COMPLETE'}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div
          style={{
            borderTop: '1px solid #dfe4ec',
            color: '#4f596b',
            display: 'flex',
            fontFamily: 'monospace',
            fontSize: 17,
            gap: 34,
            paddingTop: 20,
          }}
        >
          <span>04 SURFACES</span>
          <span>18 TOOLS</span>
          <span>33 CHANNEL CASES</span>
          <span style={{ color: '#986d16', display: 'flex', marginLeft: 'auto' }}>IMPLEMENTATION ≠ LIVE</span>
        </div>
      </div>
    ),
    size,
  );
}
