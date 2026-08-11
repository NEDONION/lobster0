import type { AttachmentRef } from "../common/api";

/** 当前草稿里已 stage 的附件，按用户添加顺序排列。 */
export type AttachmentDraft = readonly AttachmentRef[];

export function addAttachment(draft: AttachmentDraft, entry: AttachmentRef): AttachmentRef[] {
  // Store 是 content-addressed 的，同内容的文件拿到同一个 id；
  // 重复添加不该出现两个 chip，保留先添加的那条。
  if (draft.some((item) => item.artifactId === entry.artifactId)) {
    return [...draft];
  }
  return [...draft, entry];
}

export function removeAttachment(draft: AttachmentDraft, artifactId: string): AttachmentRef[] {
  return draft.filter((item) => item.artifactId !== artifactId);
}

export function attachmentIds(draft: AttachmentDraft): string[] {
  return draft.map((item) => item.artifactId);
}

export function formatAttachmentSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
