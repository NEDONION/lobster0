import { describe, expect, it } from "vitest";

import type { AttachmentRef } from "../src/common/api";
import {
  addAttachment,
  attachmentIds,
  formatAttachmentSize,
  removeAttachment,
} from "../src/renderer/attachment-draft";

function ref(id: string, filename = "note.txt", sizeBytes = 1024): AttachmentRef {
  return { artifactId: id, filename, mediaType: "text/plain", sizeBytes };
}

describe("attachment draft", () => {
  it("appends in the order the user added them", () => {
    const draft = addAttachment(addAttachment([], ref("a")), ref("b", "shot.png"));
    expect(attachmentIds(draft)).toEqual(["a", "b"]);
  });

  it("keeps one entry per artifact id", () => {
    // 同内容的文件在 Store 里是同一个 id；重复添加不该出现两个 chip。
    const draft = addAttachment(addAttachment([], ref("a", "one.txt")), ref("a", "two.txt"));
    expect(draft).toHaveLength(1);
    expect(draft[0]?.filename).toBe("one.txt");
  });

  it("removes only the targeted entry", () => {
    const draft = addAttachment(addAttachment([], ref("a")), ref("b"));
    expect(attachmentIds(removeAttachment(draft, "a"))).toEqual(["b"]);
    expect(attachmentIds(removeAttachment(draft, "missing"))).toEqual(["a", "b"]);
  });

  it("never mutates the input array", () => {
    const original = addAttachment([], ref("a"));
    addAttachment(original, ref("b"));
    removeAttachment(original, "a");
    expect(attachmentIds(original)).toEqual(["a"]);
  });

  it("formats sizes at a human scale", () => {
    expect(formatAttachmentSize(512)).toBe("512 B");
    expect(formatAttachmentSize(2048)).toBe("2.0 KB");
    expect(formatAttachmentSize(5 * 1024 * 1024)).toBe("5.0 MB");
  });
});
