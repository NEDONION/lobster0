/**
 * `window.lobster0` 的 Web 实现：把 Electron IPC 换成 HTTP POST + SSE。
 *
 * Renderer 完全不知道自己跑在浏览器里——它拿到的仍然是同一个
 * `createDesktopApi` 产物，只是底下的两个函数换了传输。
 */

import { DESKTOP_CHANNELS, createDesktopApi, type DesktopApi } from "../common/api";
import {
  CONSOLE_HEADER,
  CONSOLE_HEADER_VALUE,
  WEB_ENDPOINTS,
  WebRequestError,
  type InvokeResponse,
} from "./protocol";

/** SSE 连接的最小能力；抽出来是为了让测试不依赖浏览器的 EventSource。 */
export interface EventStream {
  addEventListener(type: "message", handler: (event: { data: string }) => void): void;
  close(): void;
}

export interface WebTransportOptions {
  /** 默认使用全局 fetch；测试注入假实现。 */
  fetch?: typeof globalThis.fetch;
  /** 默认构造浏览器 EventSource；测试注入假实现。 */
  openEventStream?: (url: string) => EventStream;
}

/**
 * 构造 Web 传输版的 DesktopApi。
 *
 * @param options 可注入的 fetch 与 SSE 工厂，默认取浏览器全局实现。
 * @returns 与 Electron preload 完全同形的 DesktopApi。
 */
export function createWebDesktopApi(options: WebTransportOptions = {}): DesktopApi {
  const fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
  const openEventStream =
    options.openEventStream
    ?? ((url: string) =>
      new EventSource(url, { withCredentials: true }) as unknown as EventStream);

  return createDesktopApi(
    async (channel, payload) => invoke(fetchImpl, channel, payload),
    (channel, handler) => subscribe(openEventStream, channel, handler),
  );
}

async function invoke(
  fetchImpl: typeof globalThis.fetch,
  channel: string,
  payload: unknown,
): Promise<unknown> {
  let response: Response;
  try {
    response = await fetchImpl(WEB_ENDPOINTS.invoke, {
      method: "POST",
      // 同源 cookie 才会被带上；跨源请求拿不到 session。
      credentials: "same-origin",
      headers: {
        "content-type": "application/json",
        [CONSOLE_HEADER]: CONSOLE_HEADER_VALUE,
      },
      // channel 放在请求体里：路径上不出现可变段，也就没有路径解析面。
      body: JSON.stringify(payload === undefined ? { channel } : { channel, payload }),
    });
  } catch {
    throw new WebRequestError("web_transport", "无法连接本地 Lobster0 控制台");
  }
  if (response.status === 401) {
    throw new WebRequestError("web_unauthorized", "控制台会话已失效，请重新载入页面");
  }
  let envelope: InvokeResponse;
  try {
    envelope = (await response.json()) as InvokeResponse;
  } catch {
    throw new WebRequestError("web_protocol", "控制台返回了无效数据");
  }
  if (!envelope || typeof envelope !== "object" || typeof envelope.ok !== "boolean") {
    throw new WebRequestError("web_protocol", "控制台返回了无效数据");
  }
  if (!envelope.ok) {
    throw new WebRequestError(envelope.code, envelope.message);
  }
  return envelope.value;
}

function subscribe(
  openEventStream: (url: string) => EventStream,
  channel: string,
  handler: (value: unknown) => void,
): () => void {
  // 契约里只有一条推送 channel；出现别的说明两侧已经漂移，早失败好过静默沉默。
  if (channel !== DESKTOP_CHANNELS.frame) {
    throw new WebRequestError("web_protocol", `未知的推送通道 ${channel}`);
  }
  const stream = openEventStream(WEB_ENDPOINTS.frames);
  stream.addEventListener("message", (event) => {
    let frame: unknown;
    try {
      frame = JSON.parse(event.data);
    } catch {
      // 单条坏帧不该让整条流失效，其余帧仍然有意义。
      return;
    }
    handler(frame);
  });
  return () => stream.close();
}
