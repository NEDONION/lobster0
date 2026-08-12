import { useState } from "react";

import type { ArtifactPreview, ArtifactSummary } from "../common/api";
import { formatAttachmentSize } from "./attachment-draft";

interface ArtifactPanelProps {
  artifacts: ArtifactSummary[];
  error: string | null;
  /** Core 是否开放了「在访达中显示」；未开放时不显示该入口。 */
  canReveal: boolean;
  onPreview: (artifactId: string) => Promise<ArtifactPreview>;
  onReveal: (artifactId: string) => Promise<void>;
}

const ORIGIN_LABELS: Record<string, string> = {
  user_upload: "我上传",
  agent_output: "Agent 产生",
};

export function ArtifactPanel({
  artifacts,
  error,
  canReveal,
  onPreview,
  onReveal,
}: ArtifactPanelProps): React.JSX.Element {
  return (
    <aside className="artifact-panel" aria-label="共享产物">
      <h2 className="artifact-title">产物</h2>
      {error ? <p className="panel-error" role="alert">{error}</p> : null}
      {artifacts.length === 0 ? (
        <p className="artifact-empty">暂无产物。</p>
      ) : (
        <ul className="artifact-list">
          {artifacts.map((item) => (
            <ArtifactCard
              canReveal={canReveal}
              item={item}
              key={item.artifactId}
              onPreview={onPreview}
              onReveal={onReveal}
            />
          ))}
        </ul>
      )}
    </aside>
  );
}

function ArtifactCard({
  item,
  canReveal,
  onPreview,
  onReveal,
}: {
  item: ArtifactSummary;
  canReveal: boolean;
  onPreview: (artifactId: string) => Promise<ArtifactPreview>;
  onReveal: (artifactId: string) => Promise<void>;
}): React.JSX.Element {
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);

  async function toggle(): Promise<void> {
    if (preview !== null) {
      setPreview(null);
      return;
    }
    setBusy(true);
    setFailed(false);
    try {
      setPreview(await onPreview(item.artifactId));
    } catch {
      // 一条产物预览失败不该让整个右栏不可用。
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="artifact-card" data-artifact-id={item.artifactId}>
      <div className="artifact-card-head">
        {/* Agent 产生的截图没有文件名，退回显示类型而不是留一行空白。 */}
        <span className="artifact-name">{item.filename ?? item.mediaType}</span>
        <span className="artifact-origin">{ORIGIN_LABELS[item.origin] ?? item.origin}</span>
      </div>
      <p className="artifact-meta">
        {item.mediaType} · {formatAttachmentSize(item.sizeBytes)}
      </p>
      <div className="artifact-actions">
        <button className="button-secondary" disabled={busy} onClick={() => void toggle()} type="button">
          {preview === null ? "预览" : "收起"}
        </button>
        {canReveal ? (
          <button
            className="button-secondary"
            onClick={() => void onReveal(item.artifactId)}
            type="button"
          >
            在访达中显示
          </button>
        ) : null}
      </div>
      {failed ? <p className="artifact-error" role="alert">预览失败。</p> : null}
      {preview ? <ArtifactPreviewBody preview={preview} /> : null}
    </li>
  );
}

function ArtifactPreviewBody({ preview }: { preview: ArtifactPreview }): React.JSX.Element {
  return (
    <div className="artifact-preview">
      {preview.dataUri ? (
        <img alt="产物预览" className="artifact-image" src={preview.dataUri} />
      ) : null}
      {preview.text !== undefined ? (
        // 按文本渲染：产物内容不可信，绝不以 HTML 解释。
        <pre className="artifact-text">{preview.text}</pre>
      ) : null}
      {preview.text === undefined && preview.dataUri === undefined ? (
        <p className="artifact-meta">该类型不支持内嵌预览。</p>
      ) : null}
      {preview.truncated ? <p className="artifact-meta">内容较大，仅显示开头部分。</p> : null}
    </div>
  );
}
