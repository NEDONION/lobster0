/**
 * Web 控制台的 Node server：拥有 Python Bridge 生命周期，并把同一份
 * DesktopApi 契约用 HTTP POST + SSE 暴露出去。
 *
 * 这里是「谁能访问这台 Agent」的**唯一**权威判定点。Python launcher 也做同样的
 * 判定，但那只是提前给出更好的错误信息——直接 `node index.js` 起进程时，
 * 必须由本文件独立拒绝无保护的非回环绑定。
 */

import { randomUUID, timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { readFile } from "node:fs/promises";
import { isIP } from "node:net";
import { extname, resolve, sep } from "node:path";

import type { ServerFrame } from "@lobster0/pi-tui/protocol";

import type { BridgeService } from "../main/bridge-service";
import { DesktopRequestError, registerDesktopIpc } from "../main/ipc";
import {
  CONSOLE_HEADER,
  CONSOLE_HEADER_VALUE,
  SESSION_COOKIE,
  WEB_ENDPOINTS,
  type InvokeResponse,
} from "./protocol";

export const DEFAULT_WEB_PORT = 4180;
/** 非回环绑定的共享密钥下限；短于此等于没有保护。 */
export const MINIMUM_TOKEN_LENGTH = 32;
const MAXIMUM_BODY_BYTES = 4 * 1024 * 1024;
const HEARTBEAT_MILLISECONDS = 25_000;

export class ConsoleConfigError extends Error {
  public constructor(message: string) {
    super(message);
    this.name = "ConsoleConfigError";
  }
}

export interface ConsoleServerOptions {
  host: string;
  port: number;
  /** null 表示回环绑定、无需凭据；非 null 表示非回环绑定且必须持有它。 */
  token: string | null;
}

export interface ConsoleServer {
  readonly server: Server;
  listen(): Promise<{ host: string; port: number }>;
  close(): Promise<void>;
}

export interface ConsoleServerDependencies {
  bridge: BridgeService;
  options: ConsoleServerOptions;
  /** 已构建的浏览器产物目录；缺省时不提供静态资源，只提供 API。 */
  staticRoot?: string | undefined;
  log?: ((message: string) => void) | undefined;
}

type HostClass = "loopback" | "public";

/**
 * 判定绑定地址是回环还是对网络可达。
 *
 * 只接受无歧义的字面量：主机名要靠 DNS 才知道绑到哪，`2130706433` 这类非点分
 * 写法在不同解析实现下含义不同。两者都无法用来判断可达性，因此一律拒绝。
 *
 * @param host 绑定地址。
 * @returns `"loopback"` 或 `"public"`。
 * @throws ConsoleConfigError 地址不是 localhost 或 IP 字面量。
 */
export function classifyHost(host: string): HostClass {
  if (typeof host !== "string" || host !== host.trim() || host.length === 0) {
    throw new ConsoleConfigError("绑定地址必须是 localhost 或 IP 字面量");
  }
  if (host === "localhost") {
    return "loopback";
  }
  const family = isIP(host);
  if (family === 0) {
    throw new ConsoleConfigError(
      `无法判断 ${host} 绑到哪里；绑定地址只接受 localhost 或 IP 字面量`,
    );
  }
  if (family === 4) {
    return host.startsWith("127.") ? "loopback" : "public";
  }
  // IPv6 只承认最直白的两种回环写法。任何拿不准的写法都归为 public，
  // 也就是「必须有 token」——判错方向必须偏向更严，而不是更松。
  return host === "::1" || host === "0:0:0:0:0:0:0:1" ? "loopback" : "public";
}

/**
 * 从环境解析绑定选项，并拒绝任何无保护的非回环绑定。
 *
 * @param environment 进程环境。
 * @returns 已校验的绑定选项。
 * @throws ConsoleConfigError 端口越界，或非回环绑定缺少足够长的 token。
 */
export function resolveServerOptions(environment: NodeJS.ProcessEnv): ConsoleServerOptions {
  const host = environment.LOBSTER0_WEB_HOST?.trim() || "127.0.0.1";
  const rawPort = environment.LOBSTER0_WEB_PORT?.trim();
  const port = rawPort ? Number(rawPort) : DEFAULT_WEB_PORT;
  if (!Number.isSafeInteger(port) || port < 1024 || port > 65_535) {
    throw new ConsoleConfigError("绑定端口必须落在 1024～65535");
  }
  const token = environment.LOBSTER0_WEB_TOKEN?.trim() ?? "";
  if (classifyHost(host) === "loopback") {
    // 回环绑定不需要凭据；即使环境里有 token 也不改变语义。
    return { host, port, token: null };
  }
  if (token.length < MINIMUM_TOKEN_LENGTH) {
    throw new ConsoleConfigError(
      `绑定 ${host} 会让这台 Agent 对网络可达，必须先在环境变量 LOBSTER0_WEB_TOKEN 里`
      + `提供至少 ${MINIMUM_TOKEN_LENGTH} 个字符的共享密钥；不提供则拒绝启动，不会回退到回环`,
    );
  }
  return { host, port, token };
}

/**
 * 构造 Web 控制台 server。
 *
 * @param dependencies Bridge、已校验的绑定选项、静态资源目录与日志函数。
 * @returns 可 listen/close 的 server 句柄。
 */
export function createConsoleServer(dependencies: ConsoleServerDependencies): ConsoleServer {
  const { bridge, options } = dependencies;
  const staticRoot = dependencies.staticRoot ? resolve(dependencies.staticRoot) : null;
  const log = dependencies.log ?? (() => undefined);
  const handlers = new Map<string, (payload: unknown) => Promise<unknown>>();
  const streams = new Set<ServerResponse>();
  const sessions = new Set<string>();
  let closing = false;
  // Host 白名单要比对**实际**绑定的端口：options.port 允许为 0（由内核分配）。
  let boundPort = options.port;

  const removeFrameHandler = registerDesktopIpc(
    (channel, handler) => {
      handlers.set(channel, handler);
    },
    bridge,
    (frame) => broadcast(frame),
    // 浏览器里没有原生目录/文件选择器。返回 null 就是契约里「用户取消了」的语义，
    // Renderer 早已正确处理。刻意不让浏览器传任意路径进来——那会把「读取宿主机
    // 任意文件」这个原语交到浏览器侧。
    async () => null,
    async () => null,
  );

  function broadcast(frame: ServerFrame): void {
    // SSE 在单条连接上严格保序，且这里是同步 write，因此帧顺序与 Bridge 输出一致。
    const payload = `data: ${JSON.stringify(frame)}\n\n`;
    for (const stream of streams) {
      stream.write(payload);
    }
  }

  const heartbeat = setInterval(() => {
    for (const stream of streams) {
      stream.write(": ping\n\n");
    }
  }, HEARTBEAT_MILLISECONDS);
  heartbeat.unref();

  const server = createServer((request, response) => {
    void handle(request, response).catch(() => {
      respondJson(response, 500, { ok: false, code: "web_internal", message: "控制台内部错误" });
    });
  });

  async function handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    if (closing) {
      respondJson(response, 503, { ok: false, code: "web_closing", message: "控制台正在关闭" });
      return;
    }
    if (!hostAllowed(request)) {
      // DNS rebinding 打过来的请求 Host 是攻击者的域名，在这里被挡下。
      respondText(response, 403, "forbidden host");
      return;
    }
    if (!originAllowed(request)) {
      respondText(response, 403, "forbidden origin");
      return;
    }

    // base 用固定值：只取 pathname，不该让一个敌意的 Host 头把 URL 构造弄崩。
    const path = new URL(request.url ?? "/", "http://console.invalid").pathname;

    if (path === WEB_ENDPOINTS.session) {
      await handleSession(request, response);
      return;
    }
    if (path === WEB_ENDPOINTS.invoke) {
      await handleInvoke(request, response);
      return;
    }
    if (path === WEB_ENDPOINTS.frames) {
      handleFrames(request, response);
      return;
    }
    await handleStatic(request, response, path);
  }

  function hostAllowed(request: IncomingMessage): boolean {
    if (options.token !== null) {
      // 非回环绑定时 owner 可能用任意主机名访问；此时的保护是 token + session
      // cookie，而不是 Host 白名单。
      return true;
    }
    const host = request.headers.host;
    if (typeof host !== "string") {
      return false;
    }
    return (
      host === `127.0.0.1:${boundPort}`
      || host === `localhost:${boundPort}`
      || host === `[::1]:${boundPort}`
    );
  }

  function originAllowed(request: IncomingMessage): boolean {
    const origin = request.headers.origin;
    if (origin === undefined) {
      // 同源导航与 EventSource 不带 Origin；缺失本身不是跨源信号。
      return true;
    }
    if (typeof origin !== "string") {
      return false;
    }
    const host = request.headers.host;
    return typeof host === "string" && origin === `http://${host}`;
  }

  function authorized(request: IncomingMessage): boolean {
    if (options.token === null) {
      return true;
    }
    const session = readCookie(request.headers.cookie, SESSION_COOKIE);
    return session !== null && sessions.has(session);
  }

  async function handleSession(
    request: IncomingMessage,
    response: ServerResponse,
  ): Promise<void> {
    if (request.method !== "POST" || !consoleHeaderPresent(request)) {
      respondText(response, 403, "forbidden");
      return;
    }
    if (options.token === null) {
      // 回环模式没有 token 可换；直接告诉页面不需要登录。
      respondJson(response, 200, { ok: true, value: { required: false } });
      return;
    }
    if (authorized(request)) {
      // 已经持有有效 session 的页面重载时不该被要求重新输入密钥。
      respondJson(response, 200, { ok: true, value: { required: true } });
      return;
    }
    const body = await readBody(request);
    let supplied: unknown;
    try {
      supplied = (JSON.parse(body) as { token?: unknown }).token;
    } catch {
      respondJson(response, 400, { ok: false, code: "web_protocol", message: "请求体无效" });
      return;
    }
    if (typeof supplied !== "string" || !tokenMatches(supplied, options.token)) {
      // 失败不记录任何提交的值。
      log("console: rejected a session request with an invalid token");
      respondJson(response, 401, { ok: false, code: "web_unauthorized", message: "密钥不正确" });
      return;
    }
    // 浏览器拿到的是本进程随机生成的 session id，不是长期 token 本身。
    const session = randomUUID();
    sessions.add(session);
    response.setHeader(
      "set-cookie",
      `${SESSION_COOKIE}=${session}; HttpOnly; SameSite=Strict; Path=/`,
    );
    respondJson(response, 200, { ok: true, value: { required: true } });
  }

  async function handleInvoke(
    request: IncomingMessage,
    response: ServerResponse,
  ): Promise<void> {
    if (request.method !== "POST" || !consoleHeaderPresent(request)) {
      respondText(response, 403, "forbidden");
      return;
    }
    if (!authorized(request)) {
      respondJson(response, 401, {
        ok: false,
        code: "web_unauthorized",
        message: "控制台会话已失效",
      });
      return;
    }
    let parsed: { channel?: unknown; payload?: unknown };
    try {
      parsed = JSON.parse(await readBody(request)) as { channel?: unknown; payload?: unknown };
    } catch {
      respondJson(response, 400, { ok: false, code: "web_protocol", message: "请求体无效" });
      return;
    }
    const channel = parsed?.channel;
    // handlers 由 registerDesktopIpc 填充，因此这里天然是白名单，
    // 浏览器无法触达契约之外的任何东西。
    const handler = typeof channel === "string" ? handlers.get(channel) : undefined;
    if (handler === undefined) {
      respondJson(response, 404, {
        ok: false,
        code: "web_unknown_channel",
        message: "未知的控制台通道",
      });
      return;
    }
    try {
      const value = await handler(parsed.payload);
      respondJson(response, 200, { ok: true, value: value === undefined ? null : value });
    } catch (error) {
      respondJson(response, 200, { ok: false, ...describeError(error) });
    }
  }

  function handleFrames(request: IncomingMessage, response: ServerResponse): void {
    // EventSource 无法设置自定义头，所以这条端点不能要求 CONSOLE_HEADER；
    // 它的跨源保护来自「不输出任何 CORS 头」加上 Host/Origin 校验。
    if (request.method !== "GET") {
      respondText(response, 405, "method not allowed");
      return;
    }
    if (!authorized(request)) {
      respondText(response, 401, "unauthorized");
      return;
    }
    response.writeHead(200, {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-store",
      connection: "keep-alive",
      "x-content-type-options": "nosniff",
    });
    response.write(": open\n\n");
    streams.add(response);
    const drop = (): void => {
      streams.delete(response);
    };
    request.on("close", drop);
    response.on("close", drop);
    response.on("error", drop);
  }

  async function handleStatic(
    request: IncomingMessage,
    response: ServerResponse,
    path: string,
  ): Promise<void> {
    if (request.method !== "GET" || staticRoot === null) {
      respondText(response, 404, "not found");
      return;
    }
    const relative = path === "/" ? "index.html" : path.replace(/^\/+/, "");
    const target = resolve(staticRoot, relative);
    // 解析之后再比对前缀，`..`、编码过的斜杠和符号链接式写法都逃不出去。
    if (target !== staticRoot && !target.startsWith(staticRoot + sep)) {
      respondText(response, 403, "forbidden path");
      return;
    }
    const contentType = CONTENT_TYPES[extname(target).toLowerCase()];
    if (contentType === undefined) {
      respondText(response, 404, "not found");
      return;
    }
    let content: Buffer;
    try {
      content = await readFile(target);
    } catch {
      respondText(response, 404, "not found");
      return;
    }
    response.writeHead(200, {
      "content-type": contentType,
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      "x-frame-options": "DENY",
      "referrer-policy": "no-referrer",
      "content-security-policy":
        "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
        + "style-src 'self' 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
        + "frame-ancestors 'none'",
    });
    response.end(content);
  }

  return {
    server,
    listen: () =>
      new Promise((resolvePromise, rejectPromise) => {
        server.once("error", rejectPromise);
        server.listen(options.port, options.host, () => {
          server.removeListener("error", rejectPromise);
          const address = server.address();
          const bound =
            typeof address === "object" && address !== null
              ? { host: address.address, port: address.port }
              : { host: options.host, port: options.port };
          boundPort = bound.port;
          log(`console: listening on http://${options.host}:${bound.port}`);
          resolvePromise(bound);
        });
      }),
    close: async () => {
      closing = true;
      clearInterval(heartbeat);
      removeFrameHandler();
      for (const stream of streams) {
        stream.end();
      }
      streams.clear();
      sessions.clear();
      await new Promise<void>((resolvePromise) => {
        server.close(() => resolvePromise());
        // 保活的 SSE 连接已经 end，剩下的 keep-alive socket 不该拖住关闭。
        server.closeAllConnections?.();
      });
      // Bridge 是本 server 拥有的子进程，任何退出路径都必须回收它。
      await bridge.stop();
    },
  };
}

const CONTENT_TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
};

function consoleHeaderPresent(request: IncomingMessage): boolean {
  return request.headers[CONSOLE_HEADER] === CONSOLE_HEADER_VALUE;
}

function tokenMatches(supplied: string, expected: string): boolean {
  const left = Buffer.from(supplied, "utf8");
  const right = Buffer.from(expected, "utf8");
  // timingSafeEqual 要求等长；长度不等直接判否，只泄漏长度本身。
  return left.length === right.length && timingSafeEqual(left, right);
}

function readCookie(header: string | undefined, name: string): string | null {
  if (typeof header !== "string") {
    return null;
  }
  for (const part of header.split(";")) {
    const separator = part.indexOf("=");
    if (separator === -1) {
      continue;
    }
    if (part.slice(0, separator).trim() === name) {
      return part.slice(separator + 1).trim();
    }
  }
  return null;
}

async function readBody(request: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of request) {
    const buffer = chunk as Buffer;
    total += buffer.length;
    if (total > MAXIMUM_BODY_BYTES) {
      throw new DesktopRequestError("web_payload_too_large", "请求体过大");
    }
    chunks.push(buffer);
  }
  return Buffer.concat(chunks).toString("utf8");
}

/** 把内部错误投影成只含 code 与可展示文案的信封，绝不外泄栈或路径。 */
function describeError(error: unknown): { code: string; message: string } {
  if (error instanceof DesktopRequestError) {
    return { code: error.code, message: error.message };
  }
  if (
    typeof error === "object"
    && error !== null
    && typeof (error as { code?: unknown }).code === "string"
    && typeof (error as { message?: unknown }).message === "string"
  ) {
    // BridgeRequestError 也是带稳定 code 的，与 Electron 侧行为保持一致。
    return {
      code: (error as { code: string }).code,
      message: (error as { message: string }).message,
    };
  }
  return { code: "web_internal", message: "控制台内部错误" };
}

function respondJson(response: ServerResponse, status: number, body: InvokeResponse): void {
  if (response.headersSent) {
    return;
  }
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  response.end(JSON.stringify(body));
}

function respondText(response: ServerResponse, status: number, body: string): void {
  if (response.headersSent) {
    return;
  }
  response.writeHead(status, {
    "content-type": "text/plain; charset=utf-8",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  response.end(body);
}
