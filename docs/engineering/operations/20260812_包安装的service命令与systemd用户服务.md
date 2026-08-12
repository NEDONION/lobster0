# 包安装的 `service` 命令与 systemd 用户服务

> 状态：`IMPLEMENTED`（2026-08-12）。修复实机部署暴露的产品缺陷：按 README 首推方式
> （`uv tool install lobster0_agent-<version>-py3-none-any.whl[feishu]`）安装后，
> `lobster0 service install` 必然失败，推荐安装路径与服务安装路径互斥。

## 1. 缺陷

腾讯云 Ubuntu 24.04 实机部署中：

```console
$ lobster0 --home ~/.lobster0 service install
error: service_repository_dirty
```

根因是 `lobster0 service` 此前只有两条实现路径：

| 模式 | 判定 | 实现 | 平台 |
| --- | --- | --- | --- |
| 受管安装（installer receipt） | `resolve_install_facts(...).managed` | `install/service.py` 的 systemd-user / LaunchAgent | Linux + macOS |
| 其它一切 | 兜底 | `gateway_service.py` 的 LaunchAgent + Git provenance | 仅 macOS |

wheel / `uv tool` 安装既没有 install receipt，也没有 Git 工作树，于是落进第二条路径：

- `_resolve_repository_commit` 在 cwd 里跑 `git rev-parse HEAD` / `git status --porcelain`，
  没有仓库就抛 `service_repository_dirty`；
- 即便绕过它，`gateway_service.py` 在 `sys.platform != "darwin"` 时也会硬失败，
  它只会渲染 LaunchAgent。

也就是说，Linux 上的包安装用户**没有任何**可用的常驻方式，只能在前台跑
`lobster0 gateway`，SSH 断开即死。

## 2. 决策：三态安装模式，而不是二态

原先 `InstallFacts` 只回答"受管 / 非受管"，而 Doctor 把非受管一律描述成
"source checkout"——对 wheel 安装来说这是**错的**。本次把它扩成三态，
让 Doctor 与 CLI 继续共用同一个判定，而不是各自再发明一套：

```python
class InstallMethod(StrEnum):
    MANAGED = "managed"    # 存在通过校验的 install receipt
    PACKAGE = "package"    # 从已安装的 site-packages/dist-packages 运行
    SOURCE  = "source"     # 从工作树（src 布局 / editable 安装）运行
```

### 2.1 PACKAGE 与 SOURCE 如何区分

判据是**当前 `lobster0` 模块从哪里被导入的**，而不是 cwd：

```
Path(lobster0.__file__).parent.parent.name in {"site-packages", "dist-packages"}
```

- wheel / `uv tool` / `pipx` / `pip install --user` → `.../site-packages/lobster0/` → PACKAGE
- 源码 checkout 与 `pip install -e .` → `<repo>/src/lobster0/` → SOURCE

选它的理由：

1. **与 cwd 无关。** 旧实现把 provenance 绑在 `Path.cwd()` 上，用户在哪个目录敲命令
   会改变判定结果；模块位置是进程事实，不可被工作目录影响。
2. **不能靠删 `.git` 绕过。** 如果用 "有没有 `.git`" 判定，删掉 `.git` 的源码树就会掉进
   PACKAGE 模式，等于给 provenance 检查开了后门。用模块位置判定时，源码树永远是
   SOURCE，缺 `.git` 就照旧 `service_repository_invalid` 失败——**不放宽**。
3. **降级路径保守。** `resolve_install_facts` 的三个降级分支
   （`state_home_invalid` / `account_unavailable` / `receipt_invalid`）一律保持 SOURCE，
   不会因为受管安装的 receipt 损坏就悄悄改走 PACKAGE 路径、写出一个指向 runtime venv
   而非 stable launcher 的 unit。

### 2.2 Git provenance 的作用域

**不删除**。`_service_repository_commit` 原样保留，只在 `InstallMethod.SOURCE` 下执行。
它的语义是"把 LaunchAgent 绑到一个干净的 commit 上"，这在包安装里没有对象可绑
（既没有仓库，也没有可能变脏的工作树），因此是**收窄作用域**而不是放宽保证。

## 3. 实现：复用 `install/service.py`，不写第三套渲染

`install/service.py` 已经有完整的 systemd-user + LaunchAgent 实现
（`_render_systemd`、`Restart=on-failure`、`RestartSec=5`、原子发布、
`systemd-analyze verify` lint、隔离回滚）。它唯一的入口
`render_service_spec(layout: InstallLayout, ...)` 要求一个受管 `InstallLayout`
（`program_prefix/bin/runtimes/current/...`），而包安装没有这些目录。

因此新增**同模块内**的第二个构造入口：

```python
def render_package_service_spec(*, launcher, state_home, platform, user_home=None) -> ServiceSpec
```

它构造同一个 `_ServiceEvidence`（sealed）并调用同一个 `_build_service_spec`，
于是 unit / plist 内容、argv、lint、发布与回滚逻辑**逐字复用**，没有第二份渲染代码。

生命周期由 `install/package_service.py` 驱动，只做四件受管路径由 receipt 承担的事：

- 决定 platform（`linux` → `SYSTEMD_USER`，`darwin` → `LAUNCHD`，其余拒绝）；
- 解析 `ExecStart` 的可执行文件（见 §5）；
- 维护一个极小的 owner-only 服务 receipt `<home>/run/service.json`
  （`{"label", "path", "sha256"}`），让 `service_install` / `service_uninstall`
  仍然只覆盖/删除"确实是我们写的且未被改动"的文件；
- 把 `InstallError` 翻译成稳定错误码。

## 4. 三条路径的分发

`cli.py::_run_service` 现在是一个显式三分支，而不是往兜底路径里塞特例：

```python
facts = resolve_install_facts(paths.home)
if facts.method is InstallMethod.MANAGED:  return run_install_action(f"service.{command}", ...)
if facts.method is InstallMethod.PACKAGE:  return run_package_service_action(command, paths=paths)
# InstallMethod.SOURCE：Phase 6 的 macOS LaunchAgent + Git provenance，行为与输出不变
```

`install` / `status` / `logs` / `restart` / `uninstall` 五个动作在 PACKAGE 模式下全部可用，
`logs` 在 systemd 下走 `journalctl --user-unit`，在 launchd 下 `tail` 两个日志文件。

## 5. `ExecStart` 的可执行文件解析

按固定顺序取第一个**存在、是普通文件、对当前用户可执行**的候选，绝不硬编码
`~/.local/bin/lobster0`：

1. `Path(sys.executable).parent / "lobster0"` —— venv / `uv tool` / `pipx`：
   console script 与解释器同在一个 `bin/`。这是最强的候选，因为它保证
   ExecStart 指向的正是**当前正在运行的这个安装**。
2. `sysconfig.get_path("scripts") / "lobster0"`
3. `sysconfig.get_path("scripts", scheme="posix_user") / "lobster0"` —— `pip install --user`。

刻意**不**回退到 `shutil.which("lobster0")`：PATH 上的同名命令可能属于另一个安装，
让服务指向另一个版本比失败更糟。全部候选落空时 fail closed：

```console
$ lobster0 service install
error: service_executable_unresolved
The lobster0 executable for this installation could not be located; reinstall with
`uv tool install` or `pipx install` so the console script sits beside the interpreter.
```

## 6. Secret 路径的一致性

`_render_systemd` 写 `Environment=LOBSTER0_ENV_FILE=<state_home>/secrets.env`，
而 `_canonical_service_fields` 硬性要求 `secrets_file == state_home / "secrets.env"`。
运行时 `env.resolve_dotenv_path` 在没有显式 `LOBSTER0_ENV_FILE` 时也回退到
`StatePaths.secrets_file`，即同一个 `<home>/secrets.env`。两者指向同一个文件，不打架。

为避免"检查的文件"与"服务实际读的文件"不一致，PACKAGE 模式的 preflight
**显式**按 `paths.secrets_file` 加载 dotenv，而不是按调用者环境里的
`LOBSTER0_ENV_FILE` —— 后者只影响当前这次 CLI 调用，不会写进 unit。

## 7. Feishu-only 限制的处置

源码 checkout 路径里的 `service production target must be Feishu only` **保留原样**；
PACKAGE 模式**不继承**它。理由：

1. 它不是安全或正确性不变量，而是 Phase 6 "macOS + 飞书生产验收"的**范围闸门**。
2. 与它并列的受管安装路径（`_run_managed_service`）从来就没有这条限制。
   只在包安装上加一条受管安装没有的限制，会让两条同级路径的语义互相矛盾。
3. 产品公开支持 Telegram 与 Discord，`lobster0 gateway` 前台运行也从不校验这一条；
   服务只是"同一个 gateway 放到 supervisor 下"，没有理由更严。

真正值得保留的不变量被显式化了：PACKAGE 模式要求**至少启用一个 Channel**
（否则装出来的是一个什么都不做的常驻进程），并照旧跑本地 Doctor 门禁
（豁免与 Gateway 无关的 `pi_tui` / `browser` 两项），Doctor 不全绿就在写 unit 之前拒绝。

## 8. 渲染出来的 unit

包安装（`uv tool`，state home `/home/ubuntu/.lobster0`）下的
`~/.config/systemd/user/lobster0-gateway.service`（0600）：

```ini
[Unit]
Description=Lobster0 Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/home/ubuntu/.local/share/uv/tools/lobster0-agent/bin/lobster0 gateway --home /home/ubuntu/.lobster0
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment=LOBSTER0_ENV_FILE=/home/ubuntu/.lobster0/secrets.env
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077

[Install]
WantedBy=default.target
```

`WantedBy=default.target` + `systemctl --user enable --now` 使其开机自启；
配合 `loginctl enable-linger $USER` 才能在**注销后**继续运行——这一点在
[Linux 服务器部署指南](../../getting-started/20260811_Linux服务器部署指南.md)中说明。

## 9. 顺带修掉的一个真缺陷：lint 临时名不是合法 unit 名

活体验证时 `install` 一直返回 `service_install_failed`。根因在**共享**的
`install/service.py`：`_write_temporary` 写出的校验临时文件叫
`.lobster0-gateway.service.<pid>.tmp`，而 `systemd-analyze verify` 只接受合法
unit 名：

```console
$ systemd-analyze --user verify /home/deployer/.lobster0-gateway.service.999.tmp
Failed to prepare filename ...: Invalid argument
```

于是 `_validate_temporary` 的 lint 在**任何真实 systemd 主机**上必然失败——
这不只影响新的包安装路径，**受管安装的 `service install` 在 Linux 上同样一直是坏的**。
macOS 上没有 `/usr/bin/systemd-analyze`，`_systemd_analyze_available()` 返回
False 直接跳过 lint，所以本机测试永远看不到。

修法是把后缀放到最后，仍是 dotfile、仍是 0600、仍在同一目录（`os.link` 需要同一
文件系统）：`.lobster0-gateway.<pid>.tmp.service`。已补回归测试覆盖两个平台。

## 10. 验证

**已执行**（Ubuntu 24.04 容器，systemd 255 以 PID 1 运行，`deployer` 非 root 用户，
`loginctl enable-linger` 后有真实 user manager 与 session bus）：

- 用 README 首推的方式真实安装：`uv tool install --python 3.12 "<wheel>[feishu]"`。
- `resolve_install_facts(...).method` == `package`，`detail` == `package`。
- `resolve_package_launcher()` 解析到
  `/home/deployer/.local/share/uv/tools/lobster0-agent/bin/lobster0`
  （即 `~/.local/bin/lobster0` symlink 指向的真实可执行文件）。
- `systemd-analyze --user verify` 对**真实**渲染出的 unit 退出 0、零 warning。
- 完整生命周期活体通过：`install` → `status`（installed=true running=true）→
  `restart` → `kill -9` 后 systemd 自动拉起（PID 408 → 422，`is-active=active`）→
  `uninstall`（unit 与 receipt 均被清干净，`is-enabled` 变回 `not-found`）。
- systemd 自己解析出的事实：`Restart=on-failure`、`RestartUSec=5s`、
  `WantedBy=default.target`、`is-enabled=enabled`。
- 修复前的缺陷复现：`lobster0 --home ~/.lobster0 service install` 不再返回
  `service_repository_dirty`，而是走到 preflight 并如实报
  `no_channels_enabled`（该容器里的状态根未配置任何 Channel）。

**未执行**：以真实飞书凭据让 Gateway 进程长期存活的活体 soak。活体生命周期是用一个
常驻 stub 作为 `ExecStart` 跑通的——真实 `lobster0 gateway` 在无凭据容器里会崩溃退出，
`_run_install_and_health` 会（按设计）判定 health 失败并把 unit 回滚删除。
真实凭据下的 soak 仍需 Tier 1 VM/实体 runner。
