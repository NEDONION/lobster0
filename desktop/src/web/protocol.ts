/**
 * Web 控制台传输层的固定常量，由浏览器与 Node server 共用。
 *
 * 这里没有任何 Electron 或 Node 专有类型，因此可以被两侧同时 import；
 * 常量写死在一处是为了避免端点或头名在两侧漂移。
 */

export const WEB_ENDPOINTS = {
  /** 所有 DesktopApi 请求的唯一入口；channel 放在请求体里，不放在路径上。 */
  invoke: "/api/invoke",
  /** ServerFrame 的单向 SSE 推送。 */
  frames: "/api/frames",
  /** 仅在配置了 token 时存在：用请求体换一个内存 session cookie。 */
  session: "/api/session",
} as const;

/**
 * 所有 API 请求必须携带的自定义头。
 *
 * 跨源 `fetch` 想带上自定义头就必须先过 CORS 预检，而 server 永远不输出任何
 * `Access-Control-Allow-*`，预检必然失败。这条是挡住「恶意页面用浏览器里的
 * 环境凭据调用本地 Agent」的主力。
 */
export const CONSOLE_HEADER = "x-lobster0-console";
export const CONSOLE_HEADER_VALUE = "1";

/** session cookie 名；值是 server 进程内随机生成的 id，不是 token 本身。 */
export const SESSION_COOKIE = "lobster0_console";

/** invoke 的成功/失败信封；失败时只回 code 与可展示文案，不回栈。 */
export type InvokeResponse =
  | { ok: true; value: unknown }
  | { ok: false; code: string; message: string };

export class WebRequestError extends Error {
  public readonly code: string;

  public constructor(code: string, message: string) {
    super(message);
    this.name = "WebRequestError";
    this.code = code;
  }
}
