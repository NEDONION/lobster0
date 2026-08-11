/**
 * 浏览器入口：先装好 Web 版的 `window.lobster0`，再渲染与 Electron 完全相同的 App。
 *
 * Renderer 组件一行都没有改；它拿到的 `window.lobster0` 与 preload 暴露的同形。
 */

import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";

import { App } from "../renderer/app";
import "../renderer/styles.css";
import "./console-gate.css";
import { createWebDesktopApi } from "./client";
import { CONSOLE_HEADER, CONSOLE_HEADER_VALUE, WEB_ENDPOINTS } from "./protocol";

window.lobster0 = createWebDesktopApi();

type GateState = "checking" | "ready" | "needs-token" | "rejected";

/**
 * 用共享密钥换取 session cookie。
 *
 * 密钥只出现在请求体里——绝不进 URL、绝不写入 localStorage。
 *
 * @param token 用户输入的共享密钥；不传表示只探测是否需要登录。
 * @returns server 是否接受了本次调用。
 */
async function exchangeSession(token?: string): Promise<boolean> {
  const response = await fetch(WEB_ENDPOINTS.session, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "content-type": "application/json",
      [CONSOLE_HEADER]: CONSOLE_HEADER_VALUE,
    },
    body: JSON.stringify(token === undefined ? {} : { token }),
  });
  return response.ok;
}

function ConsoleGate(): React.JSX.Element {
  const [state, setState] = useState<GateState>("checking");
  const [token, setToken] = useState("");

  useEffect(() => {
    void exchangeSession()
      .then((ok) => setState(ok ? "ready" : "needs-token"))
      .catch(() => setState("needs-token"));
  }, []);

  if (state === "ready") {
    return <App />;
  }
  if (state === "checking") {
    return <main className="console-gate">正在连接本地 Lobster0…</main>;
  }
  return (
    <main className="console-gate">
      <h1>Lobster0 控制台</h1>
      <p>这个控制台绑定在非回环地址，需要共享密钥才能继续。</p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setState("checking");
          void exchangeSession(token)
            .then((ok) => {
              setToken("");
              setState(ok ? "ready" : "rejected");
            })
            .catch(() => setState("rejected"));
        }}
      >
        <input
          type="password"
          autoComplete="off"
          value={token}
          placeholder="LOBSTER0_WEB_TOKEN"
          onChange={(event) => setToken(event.target.value)}
        />
        <button type="submit" disabled={token.length === 0}>
          进入
        </button>
      </form>
      {state === "rejected" ? <p role="alert">密钥不正确。</p> : null}
    </main>
  );
}

const root = document.getElementById("root");
if (!root) {
  throw new Error("Console root element is missing");
}

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <ConsoleGate />
  </React.StrictMode>,
);
