# Phase 2.3 工程文档：Exact-Argv 命令执行

> 状态：`run_command` 已进入生产 `chat`，默认未命中规则时生成参数绑定 Approval
>
> 当前门禁：245/245 tests、20/20 offline Agent cases、DeepSeek live smoke、Ruff PASS

## 1. 大白话解释

MiniClaw 没有“运行一段 Shell 文本”这个入口。模型必须把动作拆成程序和参数数组：

```json
{
  "program": "git",
  "args": ["status", "--short"],
  "timeout_seconds": 30
}
```

这与 `"git status --short"` 不一样：代码从不拼接字符串，不解释空格、引号、管道、重定向、`$()` 或反引号。
最终调用的是 `asyncio.create_subprocess_exec(resolved_program, *args, shell=False)`。

## 2. 完整链路

```mermaid
flowchart TD
    CALL["Model: program + args[]"] --> VALIDATE["Schema / byte / timeout validation"]
    VALIDATE --> NORMALIZE["fixed PATH + shutil.which + real executable"]
    NORMALIZE --> HARD{"硬禁止?"}
    HARD -->|"是"| DENY["tool.denied；无 Approval / ToolRun"]
    HARD -->|"否"| EXACT{"exact executable + argv 命中?"}
    EXACT -->|"命中"| RUN["create_subprocess_exec"]
    EXACT -->|"未命中；ask=on-miss"| APPROVAL["pending Approval"]
    APPROVAL -->|"approve once"| RUN
    APPROVAL -->|"approve --always"| RUN
    RUN --> RESULT["bounded stdout / stderr + exit code"]
    RESULT --> MODEL["模型基于真实结果回答"]
    RESULT -->|"成功且 --always"| RULE["policy_rules: exact_argv"]
```

硬禁止先于审批。因此 `bash -c`、`sudo`、`rm` 或 `git push` 不会生成一张“也许可以点批准”的单子。

## 3. 参数契约

| 字段 | 规则 |
| --- | --- |
| `program` | 非空字符串；按固定最小 PATH 查找，或显式绝对/Workspace 相对 executable |
| `args` | 最多 64 个字符串；边界、空参数和重复参数原样保留 |
| `timeout_seconds` | 默认 30，配置上限不超过 120，模型不能放大 |
| 完整 argv | UTF-8 大小最多 32 KiB；控制字符与 NUL 拒绝 |

不存在 `command`、`cwd`、`env`、stdin、PTY 或 background 参数。`cwd` 永远是当前 MiniClaw Workspace。

## 4. executable 规范化

裸程序名只在固定路径中查找：

```text
/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin
```

不会读取用户的动态 `PATH`。解析结果保存为绝对真实路径并进入 Approval hash；同名程序被替换成另一位置后，旧规则
不会模糊匹配。找不到程序、不可执行文件和 symlink loop 都返回脱敏 `command_not_found`。

## 5. 不可审批的命令

| 类别 | 例子 |
| --- | --- |
| Shell / 间接分发 | `sh`、`bash`、`zsh`、PowerShell、`env`、`xargs` |
| inline eval | `python -c/-m`、`node -e`、Ruby/Perl `-e` |
| 删除/直接覆盖 | `rm`、`rmdir`、`shred`、`mv`、`cp`、`truncate`、`dd`、`tee` |
| 远程/上传下载 | `ssh`、`scp`、`sftp`、`rsync`、`curl`、`wget`、`nc` |
| 提权 | `sudo`、`su`、`doas` |
| 容器/系统服务 | Docker/Podman、`systemctl`、`service`、`launchctl` |
| 包安装入口 | pip、npm、yarn、pnpm、brew、apt、yum、dnf 等 |
| Git 红线 | `push`、`clean`、`reset --hard`、`config/credential` |

通用网络读取必须走已实现的 `http_get`；文件创建和精确修改应使用带 Workspace Guard 的文件 Tool。

## 6. security × ask

命令 Policy 使用配置中的两个轴：

| security | ask | exact rule 命中 | 未命中 |
| --- | --- | --- | --- |
| `deny` | 任意 | deny | deny |
| `allowlist` | `off` | allow | deny |
| `allowlist` | `on-miss` | allow | require approval |
| `allowlist` | `always` | require approval | require approval |
| `full` | `off/on-miss` | allow | allow |
| `full` | `always` | require approval | require approval |

默认是 `allowlist + on-miss`，即首次需要 Owner 确认。

## 7. 审批时能看到什么

文件写入摘要隐藏 content；命令审批不同，它必须让 Owner 看清完整动作。`approvals show ID` 的 summary 使用无歧义
JSON argv，包含 resolved executable 和每个原始参数：

```text
summary: run_command ["/usr/bin/git","status","--short"]
```

这段 summary 只存在 owner-only SQLite 和本地 CLI，不进入普通 Audit metadata。Audit 仍只记录 Tool 名、ID 和
参数 hash 前缀。

## 8. `--always` 为什么不是“永久允许 git”

成功执行后保存的规则只有：

```json
{
  "type": "exact_argv",
  "resolved_program": "/usr/bin/git",
  "args": ["status", "--short"]
}
```

下次只有 executable 和完整 argv 同时相等才自动放行。多一个 `--porcelain`、少一个参数、顺序变化或 executable
路径变化都会重新进入审批。规则必须来源于已 `consumed` 且 ToolRun `succeeded` 的 Approval；不能凭 CLI 参数
凭空创建。

## 9. 子进程隔离

```mermaid
sequenceDiagram
    participant Tool as RunCommandTool
    participant Proc as New process group
    participant Out as stdout reader
    participant Err as stderr reader

    Tool->>Proc: exec(program, *args), cwd=Workspace, stdin=DEVNULL
    Tool->>Out: drain concurrently; keep ≤1 MiB
    Tool->>Err: drain concurrently; keep ≤1 MiB
    alt entire group + drains finish before timeout
        Proc-->>Tool: exit code
        Out-->>Tool: bounded bytes + truncated
        Err-->>Tool: bounded bytes + truncated
    else timeout / cancellation
        Tool->>Proc: SIGTERM process group
        Tool->>Proc: after 2s SIGKILL
        Tool->>Out: finish draining and close transport
        Tool->>Err: finish draining and close transport
    end
```

环境只包含固定 PATH、locale 和必要 Windows 平台变量。API Key、Token、Cookie、Secret、代理和用户
`PYTHONPATH` 都不会继承。stdout/stderr 分开保留，各最多 1 MiB；进入模型前还受 Executor 全局 20,000 字符上限。

## 10. 结果与错误

成功执行表示“进程被安全启动并结束”，不等于 exit code 必须为 0。模型会收到：program basename、原样 args、
固定 cwd、exit code、两个文本流、各自 truncated 标记和 duration。

| 错误码 | 含义 |
| --- | --- |
| `invalid_arguments` | Schema、argv 或 timeout 不合法 |
| `command_not_found` | executable 无法安全解析 |
| `command_forbidden` | 命中不可审批红线 |
| `approval_required` | 安全但未命中规则 |
| `command_failed` | 进程无法启动 |
| `tool_timeout` | 整个进程组/管道未在期限内结束 |

## 11. 测试证据

```bash
uv run python -m unittest tests.test_command_policy tests.test_run_command \
  tests.test_cli_approvals -v
uv run python -W always::ResourceWarning -m unittest tests.test_run_command -v
uv run python -m unittest discover -s tests -v
uv run ruff check .
```

覆盖：硬禁止、exact/extra argv、配置矩阵、真实 subprocess、秘密环境清理、stdin EOF、双流 1 MiB、普通超时、
后台子进程占管道、transport 回收、Approval once/always、跨进程规则恢复和 forbidden 无 ToolRun。

## 12. 已知边界

- 这是应用层 Policy，不是 OS sandbox；受批准的普通程序仍拥有当前用户本来拥有的 OS 权限。
- 新进程组能终止正常后代；恶意程序主动重新 `setsid` 逃离进程组，需要 Phase 7 的 Seatbelt/container 级隔离。
- 不提供后台任务、PTY、交互 stdin、任意 Shell、删除/移动或包安装。
- Windows 进程组终止尚未作为当前 macOS/Linux MVP 的发布门禁。
