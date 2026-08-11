import { describe, expect, it } from "vitest";

import { createDesktopApi, DESKTOP_CHANNELS } from "../src/common/api";
import { createWebDesktopApi, type EventStream } from "../src/web/client";
import { CONSOLE_HEADER, CONSOLE_HEADER_VALUE, WEB_ENDPOINTS } from "../src/web/protocol";

type FetchCall = [string, RequestInit];

/** 收集调用并返回固定信封的假 fetch。 */
function stubFetch(
  body: unknown,
  status = 200,
): { calls: FetchCall[]; fetch: typeof globalThis.fetch } {
  const calls: FetchCall[] = [];
  const fetchImpl = (async (input: unknown, init: RequestInit) => {
    calls.push([String(input), init]);
    return {
      ok: status < 400,
      status,
      json: async () => body,
    } as unknown as Response;
  }) as unknown as typeof globalThis.fetch;
  return { calls, fetch: fetchImpl };
}

class FakeEventStream implements EventStream {
  public closed = 0;
  private handler: ((event: { data: string }) => void) | null = null;

  public addEventListener(_type: "message", handler: (event: { data: string }) => void): void {
    this.handler = handler;
  }

  public close(): void {
    this.closed += 1;
  }

  public emit(data: string): void {
    this.handler?.({ data });
  }
}

describe("Web 传输实现 DesktopApi 契约", () => {
  it("exposes exactly the same surface as the Electron preload", () => {
    // 契约漂移会让 Renderer 在两种传输下行为不一致，这里逐字比对方法集。
    const electron = createDesktopApi(async () => undefined, () => () => undefined);
    const web = createWebDesktopApi({
      fetch: stubFetch({ ok: true, value: null }).fetch,
      openEventStream: () => new FakeEventStream(),
    });

    expect(Object.keys(web).sort()).toEqual(Object.keys(electron).sort());
  });

  it("posts the channel in the body with the anti-CSRF header", async () => {
    const { calls, fetch } = stubFetch({ ok: true, value: null });
    const api = createWebDesktopApi({ fetch, openEventStream: () => new FakeEventStream() });

    await api.startTurn({ sessionKey: "task-1", text: "整理报告" });

    const [url, init] = calls[0]!;
    expect(url).toBe(WEB_ENDPOINTS.invoke);
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect((init.headers as Record<string, string>)[CONSOLE_HEADER]).toBe(CONSOLE_HEADER_VALUE);
    expect(JSON.parse(init.body as string)).toEqual({
      channel: DESKTOP_CHANNELS.taskStart,
      payload: { sessionKey: "task-1", text: "整理报告" },
    });
  });

  it("never puts anything on the query string", async () => {
    const { calls, fetch } = stubFetch({ ok: true, value: null });
    const api = createWebDesktopApi({ fetch, openEventStream: () => new FakeEventStream() });

    await api.setProviderSecret("openai", "sk-secret-value");

    const [url, init] = calls[0]!;
    // 密钥必须只出现在请求体里；URL 会进浏览器历史、代理日志和 Referer。
    expect(url).not.toContain("?");
    expect(url).not.toContain("sk-secret-value");
    expect(init.body as string).toContain("sk-secret-value");
  });

  it("omits the payload key entirely for argument-less calls", async () => {
    // Electron 侧 `ipcRenderer.invoke(channel)` 让 handler 收到 undefined；
    // Core 是 exact-key 校验，多一个 payload:null 会被整体拒绝。
    const { calls, fetch } = stubFetch({ ok: true, value: {} });
    const api = createWebDesktopApi({ fetch, openEventStream: () => new FakeEventStream() });

    await api.bootstrap();

    expect(JSON.parse(calls[0]![1].body as string)).toEqual({
      channel: DESKTOP_CHANNELS.bootstrap,
    });
  });

  it("turns a failure envelope back into an error carrying the code", async () => {
    const { fetch } = stubFetch({ ok: false, code: "invalid_approval", message: "审批决定无效" });
    const api = createWebDesktopApi({ fetch, openEventStream: () => new FakeEventStream() });

    await expect(api.resolveApproval(1, "once")).rejects.toMatchObject({
      code: "invalid_approval",
    });
  });

  it("reports an expired session distinctly from a request failure", async () => {
    const { fetch } = stubFetch(null, 401);
    const api = createWebDesktopApi({ fetch, openEventStream: () => new FakeEventStream() });

    await expect(api.bootstrap()).rejects.toMatchObject({ code: "web_unauthorized" });
  });
});

describe("Web 传输的帧订阅", () => {
  it("delivers frames in the order the stream emits them", () => {
    const stream = new FakeEventStream();
    const api = createWebDesktopApi({
      fetch: stubFetch({ ok: true, value: null }).fetch,
      openEventStream: () => stream,
    });
    const received: string[] = [];

    api.onFrame((frame) => received.push((frame as { type: string }).type));
    for (const type of ["event.turn_started", "event.agent_delta", "event.turn_finished"]) {
      stream.emit(JSON.stringify({ type }));
    }

    expect(received).toEqual([
      "event.turn_started",
      "event.agent_delta",
      "event.turn_finished",
    ]);
  });

  it("drops one malformed frame without killing the stream", () => {
    const stream = new FakeEventStream();
    const api = createWebDesktopApi({
      fetch: stubFetch({ ok: true, value: null }).fetch,
      openEventStream: () => stream,
    });
    const received: unknown[] = [];

    api.onFrame((frame) => received.push(frame));
    stream.emit("{not json");
    stream.emit(JSON.stringify({ type: "event.turn_finished" }));

    expect(received).toEqual([{ type: "event.turn_finished" }]);
  });

  it("closes the stream when the subscription is disposed", () => {
    const stream = new FakeEventStream();
    const api = createWebDesktopApi({
      fetch: stubFetch({ ok: true, value: null }).fetch,
      openEventStream: () => stream,
    });

    api.onFrame(() => undefined)();

    expect(stream.closed).toBe(1);
  });

  it("opens the stream against the fixed frames endpoint", () => {
    const opened: string[] = [];
    const api = createWebDesktopApi({
      fetch: stubFetch({ ok: true, value: null }).fetch,
      openEventStream: (url) => {
        opened.push(url);
        return new FakeEventStream();
      },
    });

    api.onFrame(() => undefined);

    expect(opened).toEqual([WEB_ENDPOINTS.frames]);
  });
});
