# MiniClaw Desktop 一键启动设计

> 状态：`IMPLEMENTATION PASS / ELECTRON MANUAL PENDING`

实现证据：7/7 launcher 行为测试、942/942 Python、41/41 TUI TypeScript、28/28 Desktop tests，以及 Desktop typecheck/build、Ruff 和文档校验均通过。真实模型 LIVE smoke、Electron 鼠标/键盘视觉验收和 installer/signing 仍待完成。

## 1. 目标

在仓库根目录提供 `start-desktop.command`。macOS 用户既可在 Finder 双击，也可在终端执行同一个文件，完成项目依赖准备、MiniClaw 状态初始化和 Desktop development build 启动。

脚本解决的是 W0/W1 开发版的启动易用性，不把开发构建描述成已签名安装包。

## 2. 范围

脚本负责：

1. 从自身位置解析仓库根目录，不依赖 Finder 或终端的当前目录；
2. 检查 macOS、`uv`、Node.js `>=22.19.0` 和 Corepack；
3. 缺少 Python 虚拟环境时执行 `uv sync --extra dev`；
4. 缺少 TUI 的 `node_modules` 时按锁文件安装依赖；
5. 构建共享的 `@miniclaw/pi-tui`，确保 Desktop 安装能包含真实 `dist`；
6. 缺少 Desktop 的 `node_modules` 时按锁文件安装依赖；若本地 TUI 快照残缺，则强制刷新一次；
7. 首次使用默认状态目录时运行现有 `miniclaw setup`；
8. 已有状态目录时运行幂等 `miniclaw init`；
9. 选择现有 owner-only Secret 文件并启动 Electron development build；
10. 任一步失败时停止，显示可操作的短错误并保留终端窗口供用户查看。

脚本不负责：

- 安装 Homebrew、`uv`、Node.js 或操作系统组件；
- 把 API Key 写进脚本、命令行参数、日志或仓库 `.env`；
- 打包、签名、公证、自动更新或生成 `.dmg`；
- 启动 Gateway、修改 Channel 配置或绕过 `miniclaw setup`；
- 自动修复损坏配置、放宽文件权限或删除现有状态。

## 3. 入口与默认值

- 文件名固定为仓库根目录的 `start-desktop.command`；
- 默认状态目录沿用 Core 约定：`${MINICLAW_HOME:-$HOME/.miniclaw}`；
- `MINICLAW_HOME` 必须是绝对路径，具体校验继续由 MiniClaw Core 完成；
- Python 固定使用仓库 `.venv/bin/python`；
- 包管理固定通过 `corepack pnpm` 调用仓库锁定的 pnpm；
- 用户已显式设置的 `MINICLAW_ENV_FILE` 保持最高优先级；否则存在 `$MINICLAW_HOME/secrets.env` 时使用该文件；若两者都不存在，Bridge 仍保留仓库私密 `.env` 的现有开发语义。

## 4. 用户流程

### 4.1 首次启动

1. 用户双击 `start-desktop.command`；
2. 脚本检查系统运行时，安装缺失的 TUI 依赖并构建共享 Client；
3. 共享 Client 构建完成后，脚本才安装缺失的 Desktop 依赖；
4. 脚本调用 `miniclaw setup --home "$MINICLAW_HOME"`；
5. 现有 setup 从 `/dev/tty` 收集模型 API Key，并允许用户选择是否启用三个 Channel；
6. setup 以 `0700` 状态目录和 `0600` Secret 文件保存配置；
7. 脚本设置 Desktop 需要的进程环境并执行 `corepack pnpm --dir desktop dev`。

### 4.2 后续启动

1. 依赖目录存在时不重复联网安装，但始终快速构建共享 TUI；若 Desktop 中的本地 TUI 快照残缺，则自动刷新；
2. 已存在 `config.toml` 时不再调用 fresh-only setup；
3. 脚本调用 `miniclaw init --home "$MINICLAW_HOME"` 补齐迁移和缺失的非 Secret 状态；
4. Electron 继承当前 shell 环境和选定的 Secret 文件路径后启动。

## 5. 安全边界

- 脚本启用严格 Shell 模式，并始终对路径加引号；
- Secret 只由现有 Python setup/getpass 读取，Shell 不接收、不展开、不回显 Secret 值；
- 脚本只导出 Secret 文件的绝对路径，不读取文件内容；
- 依赖安装使用已提交的 lockfile 和 `--frozen-lockfile`；
- 不使用 `eval`、拼接 Shell 命令、`curl | sh`、`sudo` 或递归删除；
- 不更改现有状态目录和 Secret 文件权限来强行通过校验，权限错误由 Core 安全失败；
- Electron 仍由 Main 进程持有 Python Bridge，Renderer 不获得 Node.js、文件系统或进程能力。

## 6. 失败处理

| 失败 | 行为 |
|---|---|
| 非 macOS | 退出并说明当前一键入口仅支持 macOS |
| 缺少 `uv` | 退出并给出 `uv` 安装文档提示 |
| Node 版本过低或缺少 Corepack | 退出并要求 Node.js `>=22.19.0` |
| 依赖安装或 TUI 构建失败 | 保留原始工具退出码，不继续启动 Electron |
| setup 被取消 | 使用退出码 `130` 结束，不创建伪成功状态 |
| 配置、State Home 或 Secret 权限无效 | 显示 Core 的稳定错误，不尝试绕过 |
| Electron 退出 | 脚本返回 Electron 的退出码 |

失败路径打印短错误；若标准输入是交互式 TTY，则在退出前提示用户按回车关闭窗口。非交互调用不暂停并保留原始退出码；成功启动期间终端窗口与 Electron 生命周期保持绑定。

## 7. 测试与验收

新增离线 Shell 合约测试，通过临时目录和 fake `uv`、`node`、`corepack`、`miniclaw` 命令验证：

- 从任意当前目录启动仍能定位仓库；
- 缺少系统运行时会在任何项目写入前失败；
- 缺失依赖时按 Python、TUI、Desktop 的稳定顺序准备；
- 首次状态调用 `setup`，已有配置调用 `init`；
- 已有 `secrets.env` 时只传递路径，不读取或打印其中内容；
- 任一步失败后 Electron 不启动并保留退出码；
- 完整成功路径只启动一次 Desktop。

实现完成后运行：

```bash
uv run python -m unittest tests.test_desktop_launcher -v
corepack pnpm --dir tui test
MINICLAW_PYTHON=.venv/bin/python corepack pnpm --dir desktop test
corepack pnpm --dir desktop typecheck
corepack pnpm --dir desktop build
uv run ruff check .
uv run python scripts/validate_docs.py
git diff --check
```

## 8. 文档与状态

实现提交必须同步 README 的 Desktop 启动说明，将多条手工准备命令降为故障排查路径，并继续标注：

- 当前是 `W0/W1 DEVELOPMENT BUILD`；
- Electron 手工视觉与真实模型 LIVE smoke 仍待验收；
- installer/signing、Artifact 和 Sub-agent 不属于本脚本。

只有脚本测试、Desktop/TUI 门禁和一次不含真实 Secret 的进程启动 smoke 全部通过后，本文状态才可更新为 `IMPLEMENTATION PASS`。
