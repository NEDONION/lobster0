import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { AttachmentRef } from "../src/common/api";
import { MessageAttachments } from "../src/renderer/message-attachments";

const IMAGE: AttachmentRef = {
  artifactId: `art_${"a".repeat(64)}`,
  filename: "架构图.png",
  mediaType: "image/png",
  sizeBytes: 204_800,
};

const DOCUMENT: AttachmentRef = {
  artifactId: `art_${"b".repeat(64)}`,
  filename: "季度报告.txt",
  mediaType: "text/plain",
  sizeBytes: 2048,
};

describe("MessageAttachments", () => {
  it("names every attachment the message carried", () => {
    // Owner 的原话：「我上传的图片在桌面版里面为什么完全看不见？这块应该
    // 预览是要能看到我上传的是什么啊。」文件名是最低限度的答案。
    const html = renderToStaticMarkup(
      <MessageAttachments attachments={[IMAGE, DOCUMENT]} />,
    );

    expect(html).toContain("架构图.png");
    expect(html).toContain("季度报告.txt");
  });

  it("gives an image a thumbnail slot and a document a plain chip", () => {
    // 图片要能一眼看出上传的是什么，文档只需要知道带了它。
    const image = renderToStaticMarkup(<MessageAttachments attachments={[IMAGE]} />);
    const document = renderToStaticMarkup(
      <MessageAttachments attachments={[DOCUMENT]} />,
    );

    expect(image).toContain("attachment-thumb");
    expect(document).not.toContain("attachment-thumb");
  });

  it("shows a readable size instead of raw bytes", () => {
    expect(renderToStaticMarkup(<MessageAttachments attachments={[IMAGE]} />)).toContain(
      "200.0 KB",
    );
  });

  it("renders nothing at all when there are no attachments", () => {
    // 空容器会在每条消息下方留一道多余的间距。
    expect(renderToStaticMarkup(<MessageAttachments attachments={[]} />)).toBe("");
  });

  it("never exposes a filesystem path", () => {
    // D3 的边界：Renderer 永远拿不到路径。附件摘要里本来就没有，这条测试
    // 防止后续为了"点开定位文件"顺手把它加回来。
    const html = renderToStaticMarkup(
      <MessageAttachments
        attachments={[{ ...IMAGE, filename: "/Users/someone/secret/架构图.png" }]}
      />,
    );

    // 即使文件名本身被塞了一条路径，展示的也只能是最后一段。
    expect(html).not.toContain("/Users/someone");
  });
});
