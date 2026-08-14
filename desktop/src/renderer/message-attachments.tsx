import { useEffect, useState } from "react";

import type { AttachmentRef } from "../common/api";
import { formatAttachmentSize } from "./attachment-draft";

/**
 * 一条消息带的附件，显示在气泡内部。
 *
 * 为什么在气泡里而不是只在右栏：Owner 的原话是「我上传的图片在桌面版里面为什么
 * 完全看不见？……直接做成 Message 放在对话框里，而不是放在右侧面板」。右栏答的是
 * 「这个会话产生过什么」（含 Agent 自己产出的截图与下载），气泡答的是「这条消息
 * 带了什么」——两者不重复，右栏因此保留。
 *
 * 缩略图按需取：历史里只有摘要，字节要单独走 `previewArtifact`。这样一次
 * session.load 不会因为几十条带图消息变成几十兆。
 */
export function MessageAttachments({
  attachments,
}: {
  readonly attachments: readonly AttachmentRef[];
}): JSX.Element | null {
  if (attachments.length === 0) {
    // 空容器会在每条消息下方留一道多余的间距。
    return null;
  }
  return (
    <ul className="message-attachments">
      {attachments.map((item) => (
        <li className="message-attachment" key={item.artifactId}>
          {item.mediaType.startsWith("image/") ? (
            <AttachmentThumbnail attachment={item} />
          ) : null}
          <span className="attachment-name">{baseName(item.filename)}</span>
          <span className="attachment-size">{formatAttachmentSize(item.sizeBytes)}</span>
        </li>
      ))}
    </ul>
  );
}

/**
 * 图片缩略图。取不到时安静退回文件名——一张取不到的图不该让整条消息看起来坏掉。
 */
function AttachmentThumbnail({
  attachment,
}: {
  readonly attachment: AttachmentRef;
}): JSX.Element | null {
  const [dataUri, setDataUri] = useState<string | null>(null);
  const artifactId = attachment.artifactId;

  useEffect(() => {
    let cancelled = false;
    // 32KB 够一张缩略图，又不至于把大图整张读进渲染进程。
    window.lobster0
      .previewArtifact(artifactId, 32_768)
      .then((preview) => {
        if (!cancelled && preview.dataUri !== undefined) {
          setDataUri(preview.dataUri);
        }
      })
      .catch(() => {
        // 失败只影响这一张：归属校验不通过、文件已被清理、Core 未连上都会走到
        // 这里，而消息正文本身仍然是对的。
      });
    return () => {
      cancelled = true;
    };
  }, [artifactId]);

  // 容器始终渲染，尺寸由 CSS 固定：图片是异步取回的，等它到了再撑开会让整条
  // 消息跳一下。取不到就一直是空占位，而不是让整条消息看起来坏掉。
  return (
    <span className="attachment-thumb">
      {dataUri === null ? null : (
        <img alt={baseName(attachment.filename)} src={dataUri} />
      )}
    </span>
  );
}

/**
 * 只显示文件名最后一段。
 *
 * Core 已经用 `display_filename` 净化过，这里是渲染层的第二道：D3 的边界是
 * Renderer 永远不展示文件系统路径，即便某条历史数据里混进了一条。
 */
function baseName(filename: string): string {
  const segments = filename.split(/[/\\]/u);
  return segments[segments.length - 1] || filename;
}
