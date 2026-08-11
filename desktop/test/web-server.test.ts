import { afterEach, describe, expect, it } from "vitest";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { request as httpRequest } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { ServerFrame } from "@lobster0/pi-tui/protocol";

import { DESKTOP_CHANNELS } from "../src/common/api";
import type { BridgeService } from "../src/main/bridge-service";
import {
  ConsoleConfigError,
  classifyHost,
  createConsoleServer,
  resolveServerOptions,
  type ConsoleServer,
  type ConsoleServerOptions,
} from "../src/web/server";
import { CONSOLE_HEADER, CONSOLE_HEADER_VALUE, SESSION_COOKIE } from "../src/web/protocol";

const TOKEN = "t".repeat(32);

/** 只实现 registerDesktopIpc 会触达的部分，其余方法不该被调用。 */
class FakeBridge {
  public stopCalls = 0;
  public readonly started: unknown[] = [];
  private publish: ((frame: ServerFrame) => void) | null = null;

  public onFrame(handler: (frame: ServerFrame) => void): () => void {
    this.publish = handler;
    return () => {
      this.publish = null;
    };
  }

  public async start(): Promise<unknown> {
    return { model: "provider/model", workspace: "report" };
  }

  public async startTurn(input: unknown): Promise<void> {
    this.started.push(input);
  }

  public async listSessions(limit: number): Promise<unknown> {
    return [{ sessionKey: "task-1", title: "报告", updatedAt: "now", status: "completed" }, limit];
  }

  public async cancelTurn(): Promise<void> {
    throw Object.assign(new Error("当前没有运行中的任务"), { code: "turn_not_active" });
  }

  public async stop(): Promise<void> {
    this.stopCalls += 1;
  }

  public emit(frame: unknown): void {
    this.publish?.(frame as ServerFrame);
  }
}

interface RawResponse {
  status: number;
  headers: Record<string, string | string[] | undefined>;
  body: string;
}

/** 用 node:http 而不是 fetch：测试需要设置 fetch 禁止修改的 Host 头。 */
function raw(
  port: number,
  path: string,
  init: {
    method?: string;
    headers?: Record<string, string>;
    body?: string;
  } = {},
): Promise<RawResponse> {
  return new Promise((resolvePromise, rejectPromise) => {
    const clientRequest = httpRequest(
      {
        host: "127.0.0.1",
        port,
        path,
        method: init.method ?? "GET",
        headers: init.headers ?? {},
      },
      (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk: string) => {
          body += chunk;
        });
        response.on("end", () =>
          resolvePromise({ status: response.statusCode ?? 0, headers: response.headers, body }));
      },
    );
    clientRequest.on("error", rejectPromise);
    if (init.body !== undefined) {
      clientRequest.write(init.body);
    }
    clientRequest.end();
  });
}

/** 发一次带正确防 CSRF 头的 invoke。 */
function invoke(
  port: number,
  channel: string,
  payload?: unknown,
  cookie?: string,
): Promise<RawResponse> {
  const headers: Record<string, string> = {
    "content-type": "application/json",
    [CONSOLE_HEADER]: CONSOLE_HEADER_VALUE,
  };
  if (cookie !== undefined) {
    headers.cookie = cookie;
  }
  return raw(port, "/api/invoke", {
    method: "POST",
    headers,
    body: JSON.stringify(payload === undefined ? { channel } : { channel, payload }),
  });
}

/** 打开 SSE 并在收到 expected 条 data 后返回，避免依赖计时。 */
function collectFrames(port: number, expected: number, cookie?: string): Promise<string[]> {
  return new Promise((resolvePromise, rejectPromise) => {
    const headers: Record<string, string> = { accept: "text/event-stream" };
    if (cookie !== undefined) {
      headers.cookie = cookie;
    }
    const frames: string[] = [];
    const clientRequest = httpRequest(
      { host: "127.0.0.1", port, path: "/api/frames", method: "GET", headers },
      (response) => {
        if (response.statusCode !== 200) {
          rejectPromise(new Error(`sse status ${response.statusCode}`));
          return;
        }
        response.setEncoding("utf8");
        response.on("data", (chunk: string) => {
          for (const line of chunk.split("\n")) {
            if (line.startsWith("data: ")) {
              frames.push(line.slice(6));
            }
          }
          if (frames.length >= expected) {
            clientRequest.destroy();
            resolvePromise(frames);
          }
        });
      },
    );
    clientRequest.on("error", (error) => {
      if (frames.length < expected) {
        rejectPromise(error);
      }
    });
    clientRequest.end();
  });
}

let active: ConsoleServer | null = null;

async function start(
  bridge: FakeBridge,
  overrides: Partial<ConsoleServerOptions> = {},
  staticRoot?: string,
): Promise<number> {
  const server = createConsoleServer({
    bridge: bridge as unknown as BridgeService,
    // port 0 让内核分配，测试之间不会抢端口。
    options: { host: "127.0.0.1", port: 0, token: null, ...overrides },
    staticRoot,
  });
  active = server;
  const bound = await server.listen();
  return bound.port;
}

afterEach(async () => {
  await active?.close();
  active = null;
});

describe("绑定地址的判定", () => {
  it("treats only unambiguous loopback literals as loopback", () => {
    for (const host of ["127.0.0.1", "127.0.0.2", "::1", "localhost"]) {
      expect(classifyHost(host)).toBe("loopback");
    }
  });

  it("treats wildcard and routable addresses as public", () => {
    for (const host of ["0.0.0.0", "::", "192.168.1.5", "203.0.113.7"]) {
      expect(classifyHost(host)).toBe("public");
    }
  });

  it("refuses anything that is not localhost or an IP literal", () => {
    // `2130706433` 在部分 inet_aton 实现里就是 127.0.0.1，在别的实现里不是。
    // 与其猜，不如拒绝——判错方向必须偏向更严。
    for (const host of ["2130706433", "0x7f000001", "127.1", "0", "example.com", ""]) {
      expect(() => classifyHost(host)).toThrowError(ConsoleConfigError);
    }
  });
});

describe("Server 自己就是绑定保护的权威判定点", () => {
  it("defaults to loopback with no credential at all", () => {
    expect(resolveServerOptions({})).toEqual({ host: "127.0.0.1", port: 4180, token: null });
  });

  it("refuses a non-loopback bind without a token", () => {
    // 直接 `node index.js` 绕过 Python launcher 时，这里必须独立拒绝。
    for (const host of ["0.0.0.0", "::", "192.168.1.5"]) {
      expect(() => resolveServerOptions({ LOBSTER0_WEB_HOST: host })).toThrowError(
        ConsoleConfigError,
      );
    }
  });

  it("refuses a non-loopback bind with a token that is too short", () => {
    expect(() =>
      resolveServerOptions({ LOBSTER0_WEB_HOST: "0.0.0.0", LOBSTER0_WEB_TOKEN: "t".repeat(31) }),
    ).toThrowError(ConsoleConfigError);
  });

  it("never falls back to loopback when the bind is refused", () => {
    try {
      resolveServerOptions({ LOBSTER0_WEB_HOST: "0.0.0.0" });
      expect.unreachable("a token-less public bind must throw");
    } catch (error) {
      expect((error as Error).message).toContain("LOBSTER0_WEB_TOKEN");
    }
  });

  it("accepts a non-loopback bind backed by a long enough token", () => {
    expect(
      resolveServerOptions({ LOBSTER0_WEB_HOST: "0.0.0.0", LOBSTER0_WEB_TOKEN: TOKEN }),
    ).toEqual({ host: "0.0.0.0", port: 4180, token: TOKEN });
  });

  it("drops the token when bound to loopback", () => {
    expect(resolveServerOptions({ LOBSTER0_WEB_TOKEN: TOKEN }).token).toBeNull();
  });

  it("rejects privileged and out-of-range ports", () => {
    for (const port of ["80", "1023", "65536", "-1", "abc"]) {
      expect(() => resolveServerOptions({ LOBSTER0_WEB_PORT: port })).toThrowError(
        ConsoleConfigError,
      );
    }
  });
});

describe("HTTP 传输实现 DesktopApi 契约", () => {
  it("routes an invoke to the same BridgeService the Electron main uses", async () => {
    const bridge = new FakeBridge();
    const port = await start(bridge);

    const response = await invoke(port, DESKTOP_CHANNELS.taskStart, {
      sessionKey: "task-1",
      text: "整理报告",
    });

    expect(response.status).toBe(200);
    expect(JSON.parse(response.body)).toEqual({ ok: true, value: null });
    expect(bridge.started).toEqual([{ sessionKey: "task-1", text: "整理报告" }]);
  });

  it("applies the same field validation as the Electron path", async () => {
    const port = await start(new FakeBridge());

    // 空文本在 validateStartTurnInput 就被拒；Web 传输不该绕过它。
    const response = await invoke(port, DESKTOP_CHANNELS.taskStart, {
      sessionKey: "task-1",
      text: "   ",
    });

    expect(JSON.parse(response.body)).toMatchObject({ ok: false, code: "invalid_start_turn" });
  });

  it("surfaces a bridge error as a failure envelope carrying its code", async () => {
    const port = await start(new FakeBridge());

    const response = await invoke(port, DESKTOP_CHANNELS.taskCancel);

    expect(response.status).toBe(200);
    expect(JSON.parse(response.body)).toEqual({
      ok: false,
      code: "turn_not_active",
      message: "当前没有运行中的任务",
    });
  });

  it("refuses a channel that is not part of the contract", async () => {
    const port = await start(new FakeBridge());

    const response = await invoke(port, "desktop:not:a:channel");

    expect(response.status).toBe(404);
    expect(JSON.parse(response.body)).toMatchObject({ code: "web_unknown_channel" });
  });

  it("returns null instead of a host path for the native pickers", async () => {
    // 浏览器里没有原生选择器；让浏览器传任意路径会造出一个读取宿主机任意
    // 文件的原语，所以这两条一律返回「用户取消」。
    const port = await start(new FakeBridge());

    for (const channel of [DESKTOP_CHANNELS.attachmentPick, DESKTOP_CHANNELS.workspaceChoose]) {
      const response = await invoke(port, channel);
      expect(JSON.parse(response.body)).toEqual({ ok: true, value: null });
    }
  });
});

describe("跨源与 DNS rebinding 的防线", () => {
  it("rejects an API call without the custom console header", async () => {
    // 跨源 fetch 想带自定义头就要过 CORS 预检，而 server 从不输出 CORS 头。
    const port = await start(new FakeBridge());

    const response = await raw(port, "/api/invoke", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ channel: DESKTOP_CHANNELS.bootstrap }),
    });

    expect(response.status).toBe(403);
  });

  it("rejects a request whose Host header is not the loopback bind", async () => {
    // DNS rebinding 把攻击者域名解析到 127.0.0.1，但 Host 仍然是那个域名。
    const port = await start(new FakeBridge());

    const response = await raw(port, "/api/invoke", {
      method: "POST",
      headers: {
        host: "console.evil.example",
        [CONSOLE_HEADER]: CONSOLE_HEADER_VALUE,
        "content-type": "application/json",
      },
      body: JSON.stringify({ channel: DESKTOP_CHANNELS.bootstrap }),
    });

    expect(response.status).toBe(403);
    expect(response.body).toContain("host");
  });

  it("rejects a rebinding attempt on the event stream too", async () => {
    const port = await start(new FakeBridge());

    const response = await raw(port, "/api/frames", {
      headers: { host: "console.evil.example" },
    });

    expect(response.status).toBe(403);
  });

  it("rejects a cross-origin request even when the Host header is right", async () => {
    const port = await start(new FakeBridge());

    const response = await raw(port, "/api/invoke", {
      method: "POST",
      headers: {
        host: `127.0.0.1:${port}`,
        origin: "https://evil.example",
        [CONSOLE_HEADER]: CONSOLE_HEADER_VALUE,
        "content-type": "application/json",
      },
      body: JSON.stringify({ channel: DESKTOP_CHANNELS.bootstrap }),
    });

    expect(response.status).toBe(403);
    expect(response.body).toContain("origin");
  });

  it("never emits a CORS header", async () => {
    const port = await start(new FakeBridge());

    const response = await invoke(port, DESKTOP_CHANNELS.bootstrap);

    for (const header of Object.keys(response.headers)) {
      expect(header.toLowerCase()).not.toContain("access-control");
    }
  });
});

describe("SSE 推送", () => {
  it("delivers turn events in the order the bridge produced them", async () => {
    const bridge = new FakeBridge();
    const port = await start(bridge);
    const types = [
      "event.turn_started",
      "event.agent_delta",
      "event.agent_delta",
      "event.approval_required",
      "event.turn_finished",
    ];

    const collecting = collectFrames(port, types.length);
    // 等 SSE 连接注册完成再推；否则测的是竞态而不是顺序。
    await new Promise((done) => setTimeout(done, 50));
    for (const type of types) {
      bridge.emit({ type });
    }

    expect((await collecting).map((frame) => JSON.parse(frame).type)).toEqual(types);
  });

  it("stops receiving after the console is closed", async () => {
    const bridge = new FakeBridge();
    await start(bridge);

    await active!.close();
    active = null;

    // close 已经摘掉 frame handler；再推帧不该抛，也不该有订阅者。
    expect(() => bridge.emit({ type: "event.turn_finished" })).not.toThrow();
  });
});

describe("非回环绑定的 token 交换", () => {
  it("refuses an invoke without a session", async () => {
    const port = await start(new FakeBridge(), { token: TOKEN });

    const response = await invoke(port, DESKTOP_CHANNELS.bootstrap);

    expect(response.status).toBe(401);
  });

  it("refuses the event stream without a session", async () => {
    const port = await start(new FakeBridge(), { token: TOKEN });

    const response = await raw(port, "/api/frames");

    expect(response.status).toBe(401);
  });

  it("issues an HttpOnly SameSite cookie holding a session id, never the token", async () => {
    const port = await start(new FakeBridge(), { token: TOKEN });

    const response = await raw(port, "/api/session", {
      method: "POST",
      headers: { [CONSOLE_HEADER]: CONSOLE_HEADER_VALUE, "content-type": "application/json" },
      body: JSON.stringify({ token: TOKEN }),
    });

    expect(response.status).toBe(200);
    const cookie = String(response.headers["set-cookie"]);
    expect(cookie).toContain("HttpOnly");
    expect(cookie).toContain("SameSite=Strict");
    // 浏览器拿到的必须是进程内随机 session id，长期 token 不进浏览器。
    expect(cookie).not.toContain(TOKEN);
  });

  it("accepts an invoke once the session cookie is presented", async () => {
    const bridge = new FakeBridge();
    const port = await start(bridge, { token: TOKEN });
    const login = await raw(port, "/api/session", {
      method: "POST",
      headers: { [CONSOLE_HEADER]: CONSOLE_HEADER_VALUE, "content-type": "application/json" },
      body: JSON.stringify({ token: TOKEN }),
    });
    const cookie = sessionCookie(login);

    const invoked = await invoke(port, DESKTOP_CHANNELS.bootstrap, undefined, cookie);

    expect(invoked.status).toBe(200);
    expect(JSON.parse(invoked.body).ok).toBe(true);
  });

  it("rejects a wrong token and issues no cookie", async () => {
    const port = await start(new FakeBridge(), { token: TOKEN });

    const rejected = await raw(port, "/api/session", {
      method: "POST",
      headers: { [CONSOLE_HEADER]: CONSOLE_HEADER_VALUE, "content-type": "application/json" },
      body: JSON.stringify({ token: "w".repeat(32) }),
    });

    expect(rejected.status).toBe(401);
    expect(rejected.headers["set-cookie"]).toBeUndefined();
  });

  it("keeps a forged session id out", async () => {
    const port = await start(new FakeBridge(), { token: TOKEN });

    const response = await invoke(
      port,
      DESKTOP_CHANNELS.bootstrap,
      undefined,
      `${SESSION_COOKIE}=00000000-0000-4000-8000-000000000000`,
    );

    expect(response.status).toBe(401);
  });
});

/** 从登录响应里取出可直接回传的 `name=value` 片段。 */
function sessionCookie(login: RawResponse): string {
  const header = login.headers["set-cookie"];
  const first = Array.isArray(header) ? header[0] : header;
  return String(first).split(";")[0]!;
}

describe("静态资源", () => {
  it("serves the built console document and refuses to escape its root", async () => {
    const root = await mkdtemp(join(tmpdir(), "lobster0-console-"));
    await writeFile(join(root, "index.html"), "<!doctype html><title>Lobster0</title>", "utf8");
    await mkdir(join(root, "assets"));
    await writeFile(join(root, "assets/app.js"), "export {};", "utf8");
    const outside = join(root, "..", "lobster0-console-secret.txt");
    await writeFile(outside, "secret", "utf8");
    const port = await start(new FakeBridge(), {}, root);

    const document = await raw(port, "/");
    const asset = await raw(port, "/assets/app.js");
    const traversal = await raw(port, "/../lobster0-console-secret.txt");
    const encoded = await raw(port, "/%2e%2e/lobster0-console-secret.txt");

    expect(document.status).toBe(200);
    expect(document.body).toContain("Lobster0");
    expect(document.headers["content-security-policy"]).toContain("default-src 'self'");
    expect(asset.status).toBe(200);
    expect(traversal.body).not.toContain("secret");
    expect(encoded.body).not.toContain("secret");
  });

  it("refuses file types outside the served allowlist", async () => {
    const root = await mkdtemp(join(tmpdir(), "lobster0-console-"));
    await writeFile(join(root, "config.env"), "LOBSTER0_WEB_TOKEN=leak", "utf8");
    const port = await start(new FakeBridge(), {}, root);

    const response = await raw(port, "/config.env");

    expect(response.status).toBe(404);
    expect(response.body).not.toContain("leak");
  });
});

describe("Bridge 生命周期", () => {
  it("stops the Python bridge exactly once on shutdown", async () => {
    const bridge = new FakeBridge();
    await start(bridge);

    await active!.close();
    active = null;

    expect(bridge.stopCalls).toBe(1);
  });

  it("refuses new work while closing", async () => {
    const bridge = new FakeBridge();
    const port = await start(bridge);
    const closing = active!.close();
    active = null;

    await closing;

    await expect(invoke(port, DESKTOP_CHANNELS.bootstrap)).rejects.toThrow();
    expect(bridge.stopCalls).toBe(1);
  });
});
