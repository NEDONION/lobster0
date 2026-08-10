# Lobster0 安装与发布运维手册

> 适用版本：`0.7.0`；Python 分发名 `lobster0-agent`；容器 `ghcr.io/nedonion/lobster0`
>
> 当前发布状态：**RELEASE CANDIDATE / PUBLIC GATES PENDING**，逐项证据见
> [v0.7.0 一行安装候选记录](../../evals/releases/v0.7.0-install.md)

这份手册写给执行发布和处理安装事故的人。它只描述**已经实现的代码行为**和**需要人工完成的外部
配置**，不假设任何公共门禁已经通过。

## 1. 事实源与不变量

| 事实 | 唯一来源 |
| --- | --- |
| 版本号 | `src/lobster0/_version.py` |
| uv / Node / pnpm 固定版本与 SHA-256 | `release/runtime-versions.json` |
| 一行安装脚本模板 | `release/install.sh.tmpl`（由 `scripts/render_install_script.py` 渲染） |
| Release 资产清单与 hash | Release 上的 `release-manifest.json` |
| 已安装实例的受管文件所有权 | `~/.lobster0/install-receipt.json` |

不变量：

- 安装事实源是 Release 上的 immutable manifest，不是 PyPI，也不是任何镜像站；
- 一行安装不需要预装 Python、Node.js 或 pnpm，安装器自己带 pinned uv 与受管 Python 3.12；
- 新 Runtime 通过 smoke、数据库保护和 service health 之前不替换 `current`；
- 卸载默认保留全部用户数据；只有 `--purge-data` 才删除枚举出来的状态路径。

## 2. 发布前检查单

1. 工作区干净，`git status --short` 无输出；
2. `src/lobster0/_version.py`、`pyproject.toml`、目标 tag、manifest 与发布记录版本号一致；
3. 同提交本地门禁全绿（`unittest`、`ruff`、`uv build`、installer zipapp、`validate_docs.py`、
   两次 `git diff --check`）；
4. `release/runtime-versions.json` 里四个平台的 uv archive SHA-256 未漂移；
5. PyPI Trusted Publisher 与 GHCR 权限已按 §4、§5 配置好；
6. Tier 1 自托管 runner 已注册——**当前实测注册数为 0**，见候选记录 §3.1。

## 3. 草稿 Release 提升流程

发布流水线只允许按下面顺序推进，任何一步失败都停在草稿状态：

1. 推 tag `v0.7.0`，流水线构建全部资产并生成 `release-manifest.json`、`checksums.txt`、
   两份 SBOM 与 `lobster0-installer.pyz`；
2. 创建 **draft** Release 并上传资产；draft 阶段 `latest` 不指向它；
3. 跑 Tier 1 安装矩阵：每个平台组合都要覆盖 fresh install、service 安装、宿主重启恢复、
   N→N+1 升级、N-1 回滚、默认卸载；
4. 发布 PyPI（§4）与 GHCR（§5），并把实际摘要与 manifest 逐一比对；
5. 生成、附加并 attest `release-evidence.json`（契约见候选记录 §4）；
6. 全部门禁都不是 PENDING 也不是 FAIL 时，才把 draft 提升为 stable，此时
   `https://github.com/NEDONION/lobster0/releases/latest/download/install.sh` 开始生效；
7. 提升后立刻在全新 Tier 1 主机上跑一次公网 `latest` 安装冒烟。

提升后公网冒烟失败时：立即把 Release 退回 draft，记录 FAIL 证据，**不要**删除或覆盖已经不可变的
PyPI 版本，修复走 `0.7.1`。

## 4. PyPI Trusted Publisher 配置

`lobster0-agent` 使用 GitHub Actions OIDC 的 Trusted Publisher，仓库里**不存放** PyPI token：

1. 在 PyPI 项目设置里新增 pending publisher：owner `NEDONION`、repository `lobster0`、
   workflow 文件名与发布 job 一致、environment 填受保护的 `pypi`；
2. 发布 job 只在该 environment 下运行，并且只在该 job 打开 `id-token: write`；
3. 上传后立刻在干净环境校验：`pip download lobster0-agent==0.7.0`、
   `python -c "import lobster0"`、`lobster0 --version`；
4. PyPI 版本不可变：一旦上传就不能替换同版本文件。

## 5. GHCR 摘要核对

1. 镜像名固定 `ghcr.io/nedonion/lobster0`，tag 用精确版本，不用 `latest` 做事实源；
2. 推送后取回 manifest 摘要：
   ```bash
   docker buildx imagetools inspect ghcr.io/nedonion/lobster0:0.7.0
   ```
3. 该摘要必须与 `release-evidence.json` 的 `images[*].digest` 和 Release manifest 记录一致；
4. 冒烟必须**按摘要**拉取（`ghcr.io/nedonion/lobster0@sha256:...`），不能按 tag，
   并验证进程非 root、containment 生效。

### 5.1 原生 bundle 构建参数（不可省略）

`deploy/Dockerfile` 的 `LOBSTER0_REQUIRE_NATIVE_BUNDLES` 默认 `"0"`，此时
`deploy/artifacts/` 缺少 TUI/Node bundle 会**静默跳过**解包，产出不含原生包的开发镜像。
正式镜像必须显式要求原生 bundle：

```bash
docker build -f deploy/Dockerfile \
  --build-arg LOBSTER0_REQUIRE_NATIVE_BUNDLES=1 \
  --build-arg LOBSTER0_WHEEL_SHA256=<64 hex> \
  -t ghcr.io/nedonion/lobster0:0.7.0 .
```

缺 bundle 时构建会显式失败。核对方法：镜像内 `/opt/lobster0/current/native-bundles-required`
必须是 `1`，且 `/opt/lobster0/current` 下存在 TUI 与 Node 目录。开发者本机若 Node 版本低于
`22.22.3`（或 `24.15.0`）无法构建 bundle，本机镜像必然不完整，不得用于发布。

## 6. 升级、回滚与 `rollback_conflict` 人工恢复

### 6.1 受支持的升级方式

重新运行一行安装命令：

```bash
curl -fsSL --proto '=https' --tlsv1.2 \
  https://github.com/NEDONION/lobster0/releases/latest/download/install.sh | bash
```

已安装 CLI 上的 `lobster0 update` 会打印 `update_requires_bootstrap` 并以退出码 2 结束。
这是设计行为：升级流水线需要 bootstrap 建立的信任根（pinned uv 与受管 Python），而受管 Runtime
在激活时按设计删除 `.inputs`。不要试图用 `PATH` 上的 uv 绕过它。

### 6.2 自动回滚

`UpdateCoordinator` 在停掉旧 service 后备份数据库、运行新 Runtime 的迁移、原子切换、再健康检查。
任一阶段失败先停新 service，然后按 `PRAGMA data_version` 判断迁移之后有没有外部写入：

- 没有外部写入：恢复数据库备份与旧 Runtime、重启旧 service，抛出原始错误；
- 有外部写入：**不覆盖**这些数据，保留备份与当前状态，抛出 `rollback_conflict`。

### 6.3 `rollback_conflict` 的人工恢复

出现 `rollback_conflict` 说明升级失败，但用户在迁移之后已经写入了新数据；自动恢复会造成数据丢失，
所以代码故意停手。人工处理顺序：

1. **先停服务**：`lobster0 service status`，必要时 `lobster0 service uninstall`，
   避免 Gateway 继续写库；
2. **保留现场**：把 `~/.lobster0/` 下形如 `.lobster0.db.update-backup.*` 的备份文件和当前
   `lobster0.db` 一起复制到只读位置，两个都不要删；
3. **确认现状**：`~/.lobster0/current` 指向哪个 `runtimes/<version>`，
   `install-receipt.json` 里的版本是否与之一致；
4. **二选一**：
   - 保留升级后数据 → 保持当前数据库，重新运行一行安装命令把 Runtime 补齐到目标版本；
   - 放弃升级后数据 → 人工用备份覆盖 `lobster0.db`（同时删除 `-wal`/`-shm`），再重新安装 N-1 版本；
5. 恢复完成后 `lobster0 doctor`，确认无误再 `lobster0 service install`；
6. 事后把冲突时间点与选择记录进事故记录，不要让它只存在于终端历史里。

Memory、Skills、Workspace 文件不参与自动回滚，任何情况下都不要用旧备份覆盖它们。

## 7. 卸载与数据边界

```bash
lobster0 service uninstall
lobster0 uninstall
lobster0 uninstall --purge-data --yes-i-understand-data-loss
```

- 默认 `lobster0 uninstall`：停止并移除受管服务、删除受管 Runtime / launcher / receipt，
  **保留 `~/.lobster0` 下全部用户数据**，并打印保留路径与重装方法；
- `--purge-data` 只删除枚举出来的路径：`config.toml`、`secrets.env`、`lobster0.db`（含
  `-wal`/`-shm`）、`SOUL.md`、`USER.md`、`MEMORY.md`，以及 `memory/`、`prompts/`、`skills/`、
  `evals/`、`browser/`、`artifacts/`、`downloads/`、`logs/`、`run/`、`checkpoints/` 和升级备份；
- **`workspace/` 永远保留**，即使 `--purge-data` 也不删；
- 卸载器拒绝把 `/`、Home 根或 Workspace 当作删除目标，不跟随 symlink，不递归删除 receipt 之外的路径；
- 非交互 `--purge-data` 还必须附带 `--yes-i-understand-data-loss`，否则 fail closed。

## 8. 撤销与吊销

| 场景 | 动作 |
| --- | --- |
| Release 资产有问题 | 把 Release 退回 draft，使 `latest` 不再指向它；修复走新版本号 |
| PyPI 版本有问题 | 用 yank 标记该版本（yank 不删除文件，已 pin 的安装仍可解析）；修复发 `0.7.1` |
| 镜像有问题 | 删除或停止发布该 tag，并在发布记录里登记被撤销的摘要 |
| 凭据泄漏 | 立即在 PyPI 移除 Trusted Publisher 绑定、吊销 GHCR token，再排查 Release 资产是否被替换 |
| 安装脚本被替换 | 停止分发该 URL，用 `scripts/verify_release_artifacts.py` 独立复核全部资产 hash |

绝不通过重新上传同一版本号来"修复"已发布内容。

## 9. 事故排查入口

| 症状 | 首选命令 |
| --- | --- |
| 安装到一半失败 | 重跑一行安装命令并加 `--verbose`；用 `--dry-run` 查看计划 |
| 想先看计划不落盘 | 一行安装命令后追加 `-s -- --dry-run` |
| 服务不健康 | `lobster0 service status`、`lobster0 service logs`、`lobster0 doctor` |
| 升级失败 | 见 §6 |
| 平台不受支持 | 安装器在任何写入前返回 `unsupported_platform`，不会留下半安装状态 |

稳定错误码：`unsupported_platform`、`install_locked`、`artifact_download_failed`、
`artifact_hash_mismatch`、`manifest_invalid`、`system_dependency_missing`、`privilege_denied`、
`runtime_install_failed`、`tui_smoke_failed`、`service_install_failed`、`doctor_blocked`、
`activation_failed`、`rollback_conflict`、`uninstall_ownership_mismatch`、`request_invalid`、
`plan_invalid`、`installer_error`。

## 10. 相关文档

- [v0.7.0 一行安装候选记录](../../evals/releases/v0.7.0-install.md)
- [本地运行指南](../../getting-started/20260807_本地运行指南.md)
- [系统架构](../../architecture/20260807_系统架构.md)
- [工程文档索引](../README.md)
