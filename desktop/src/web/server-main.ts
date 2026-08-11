/**
 * Web 控制台的进程入口：解析绑定选项、拥有 Bridge 生命周期、处理退出信号。
 *
 * 对应 Electron 侧的 `src/main/index.ts`。
 */

import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { BridgeService } from "../main/bridge-service";
import { ConsoleConfigError, createConsoleServer, resolveServerOptions } from "./server";

const currentDirectory = dirname(fileURLToPath(import.meta.url));

async function main(): Promise<void> {
  let options;
  try {
    options = resolveServerOptions(process.env);
  } catch (error) {
    // 绑定不满足保护要求时直接退出，绝不降级为回环继续跑。
    const message = error instanceof ConsoleConfigError ? error.message : "控制台配置无效";
    process.stderr.write(`error: ${message}\n`);
    process.exitCode = 2;
    return;
  }

  const staticRoot =
    process.env.LOBSTER0_WEB_STATIC?.trim() || join(currentDirectory, "../client");
  const bridge = new BridgeService();
  const consoleServer = createConsoleServer({
    bridge,
    options,
    staticRoot,
    // 日志只写 stderr，且从不包含 token 值。
    log: (message) => process.stderr.write(`${message}\n`),
  });

  try {
    await consoleServer.listen();
  } catch (error) {
    const reason = error instanceof Error ? error.message : "未知原因";
    process.stderr.write(`error: 无法监听 ${options.host}:${options.port}（${reason}）\n`);
    process.exitCode = 5;
    await bridge.stop();
    return;
  }

  if (options.token !== null) {
    process.stderr.write(
      `warning: 控制台绑定在 ${options.host}:${options.port}，这台 Agent 现在对网络可达；`
      + "它可以执行命令、读写文件并驱动浏览器。推荐改用 SSH 端口转发。\n",
    );
  } else {
    process.stderr.write(
      "console: 仅监听回环。远程访问请使用 "
      + `ssh -N -L ${options.port}:127.0.0.1:${options.port} <host>\n`,
    );
  }

  let shuttingDown = false;
  const shutdown = (signal: NodeJS.Signals): void => {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    process.stderr.write(`console: ${signal} received, stopping Lobster0 Core\n`);
    void consoleServer
      .close()
      .catch(() => undefined)
      .finally(() => {
        process.exit(0);
      });
  };
  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));
}

void main();
