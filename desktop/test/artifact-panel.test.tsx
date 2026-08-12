import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { ArtifactSummary } from "../src/common/api";
import { ArtifactPanel } from "../src/renderer/artifact-panel";

const ITEMS: ArtifactSummary[] = [
  {
    artifactId: `art_${"a".repeat(64)}`,
    filename: "季度汇报.txt",
    mediaType: "text/plain",
    sizeBytes: 2048,
    origin: "user_upload",
    createdAt: "2026-08-11T10:00:00+00:00",
  },
  {
    artifactId: `art_${"b".repeat(64)}`,
    filename: null,
    mediaType: "image/png",
    sizeBytes: 5 * 1024 * 1024,
    origin: "agent_output",
    createdAt: "2026-08-11T11:00:00+00:00",
  },
];

function render(overrides: Partial<Parameters<typeof ArtifactPanel>[0]> = {}): string {
  return renderToStaticMarkup(
    <ArtifactPanel
      artifacts={ITEMS}
      canReveal
      error={null}
      onPreview={async () => ({
        artifactId: ITEMS[0]!.artifactId,
        mediaType: "text/plain",
        sizeBytes: 2048,
        truncated: false,
        text: "hello",
      })}
      onReveal={async () => undefined}
      {...overrides}
    />,
  );
}

describe("ArtifactPanel", () => {
  it("labels who produced each artifact", () => {
    const html = render();
    expect(html).toContain("季度汇报.txt");
    expect(html).toContain("我上传");
    expect(html).toContain("Agent 产生");
  });

  it("falls back to the media type when there is no filename", () => {
    // Agent 产生的截图没有文件名，不能显示成空白行。
    const html = render();
    expect(html).toContain("image/png");
  });

  it("renders sizes at a human scale", () => {
    const html = render();
    expect(html).toContain("2.0 KB");
    expect(html).toContain("5.0 MB");
  });

  it("shows an empty state instead of an empty box", () => {
    expect(render({ artifacts: [] })).toContain("暂无产物");
  });

  it("hides the reveal action when the Core cannot do it", () => {
    const html = render({ canReveal: false });
    expect(html).not.toContain("在访达中显示");
  });

  it("never exposes a filesystem path", () => {
    // 路径不该出现在 Renderer 里——它只在 Main 与 Core 之间流转。
    const html = render();
    expect(html).not.toMatch(/\/Users\/|file:\/\//);
  });
});
