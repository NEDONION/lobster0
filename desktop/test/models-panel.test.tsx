import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { ProviderList } from "../src/common/api";
import { ModelsPanel } from "../src/renderer/models-panel";

const LIST: ProviderList = {
  model: "deepseek-v4-pro",
  providers: [
    {
      id: "default",
      baseUrl: "https://api.deepseek.com",
      timeoutSeconds: 120,
      secretConfigured: true,
      selected: true,
    },
    {
      id: "openrouter",
      baseUrl: "https://openrouter.ai/api/v1",
      timeoutSeconds: 60,
      secretConfigured: false,
      selected: false,
    },
  ],
};

function render(overrides: Partial<Parameters<typeof ModelsPanel>[0]> = {}): string {
  return renderToStaticMarkup(
    <ModelsPanel
      busy={false}
      canWrite
      error={null}
      onRefresh={() => undefined}
      onRemove={async () => undefined}
      onSelect={async () => undefined}
      onSetSecret={async () => undefined}
      onUpsert={async () => undefined}
      providers={LIST}
      {...overrides}
    />,
  );
}

describe("ModelsPanel", () => {
  it("marks the selected provider and reports secret status per entry", () => {
    const html = render();
    expect(html).toContain("default");
    expect(html).toContain("openrouter");
    expect(html).toContain("已配置");
    expect(html).toContain("未配置");
    expect(html).toContain("默认");
  });

  it("refuses to offer deletion of the selected provider", () => {
    // 删掉当前默认项会在配置里留下悬空引用，界面上就不该给出这个入口。
    const html = render();
    const cards = html.split("data-provider-id=");
    const selectedCard = cards.find((part) => part.startsWith('"default"')) ?? "";
    expect(selectedCard).not.toContain("删除");
  });

  it("degrades to a read-only list when the Core lacks the write capability", () => {
    const html = render({ canWrite: false });
    expect(html).toContain("openrouter");
    expect(html).not.toContain("<button");
    expect(html).toContain("当前 Core 版本不支持");
  });

  it("keeps the secret field masked and never pre-fills it", () => {
    const html = render();
    expect(html).toContain('type="password"');
    // 密钥永不回流，输入框不能带任何 value。
    expect(html).not.toMatch(/type="password"[^>]*value=/);
  });

  it("warns that provider changes need a Core restart", () => {
    expect(render()).toContain("重启");
  });

  it("shows an empty state instead of an error before the first load", () => {
    const html = render({ providers: null });
    expect(html).toContain("尚未加载");
  });
});
