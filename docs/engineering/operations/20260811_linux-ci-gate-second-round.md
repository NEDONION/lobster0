# Linux 离线门禁修复（第二轮）

> 日期：2026-08-11 · 范围：`.github/workflows`、`src/lobster0/{config,tools,install}`、
> `tests/test_install_{runtime,receipt,orchestrator}.py`

## 1. 背景

首次真实发布流水线运行暴露了一个长期盲区：整套测试从未在 Linux 上跑过。1604 项里挂了
20 项。第一轮（`93d906d`）修掉 10 项，其中包含一个真产品 bug（`_prepare_regular` 过早
`os.close` 导致 inode 号可被复用，清理逻辑可能删掉他人文件）。

本轮处理剩余 10 项。所有结论都以 GitHub Actions 上 run `31437569137` 的 offline gate
原始日志为准，而不是本地复现的猜测——本地容器里另有约 43 项与环境相关的噪声，必须按
**增量**而不是绝对数量来判断。

## 2. 十项失败的根因与处置

| # | 用例 | 根因 | 处置 |
| --- | --- | --- | --- |
| 1 | `test_browser_evals::test_all_cases_pass_real_offline_components_without_sensitive_output` | CI 从不构建 `browser-worker/dist`，BROWSER-017 拿不到真实 ProfileLock | CI 配置 |
| 2 | `test_cli_eval::test_run_browser_prints_versioned_cases_and_summary` | 同上 | CI 配置 |
| 3 | `test_cli_eval::test_run_offline_prints_pass_rows_and_summary` | **产品 bug**：`_linux_cpu_model` 泄漏 `/proc/cpuinfo` handle，`ResourceWarning` 打到 stderr | 产品 |
| 4 | `test_cli_eval::test_run_channel_and_all_print_independent_gate_summaries` | 同时命中 1 与 3 | CI + 产品 |
| 5 | `test_install_runtime::test_discard_interrupted_staging_rejects_same_bytes_marker_inode_replacement` | 测试假设 unlink 后重建必得新 inode（仅 macOS 成立） | 测试 |
| 6 | `test_install_runtime::test_default_runner_builds_with_offline_fake_uv` | 测试直接把 ambient `sys.base_prefix` 当受信 tree | 测试 |
| 7 | `test_install_receipt::test_general_cleanup_quarantines_instead_of_unlinking_postcheck_replacement` | **产品 bug**：`InstallReceipt.write` / `_restore_receipt` 过早关闭描述符 | 产品 + 测试 |
| 8 | `test_install_orchestrator::test_cleanup_preserves_replaced_downloads_inode` | **产品 bug**：downloads root 只记录 `(st_dev, st_ino)`，不钉住 inode | 产品 + 测试 |
| 9 | `test_runtime::test_personal_runtime_uses_one_boundary_for_files_and_user_cli` | **产品缺陷**：`personal` profile 的默认写根被关在 `darwin` 分支里 | 产品 |
| 10 | `test_release_bundles::TuiBundleTest::setUpClass` | offline gate 没有 Node，且 `corepack pnpm` 选到自带的新版 pnpm | CI 配置 |

### 2.1 CI 配置：offline gate 缺 Node 侧前置

两个工作流的 offline gate 都只装 Python + uv。于是：

- `browser-worker/dist/profile.js` 从不存在，`_profile_lock` 直接判负；
- `tui/node_modules` 从不存在，`scripts/build_tui_bundle.py` 无从执行。

同时 `corepack pnpm --dir <project>` 在仓库根（没有 `package.json`）解析不到项目声明的
`packageManager`，会用 corepack 自带的 pnpm 11 运行，随后被 pnpm 自己以「版本不符」拒绝。
node-22 / node-24 两个 job 也栽在这里。

修法（全部是 `run:` 步骤，不引入新的 Action，遵守已审阅 SHA 名单）：

1. 按 `release/runtime-versions.json` 的固定 URL + SHA-256 装 Node 24.18.0；
2. `corepack prepare "$(读 package.json 的 packageManager)" --activate`，并在 offline gate
   里断言 `tui` 与 `browser-worker` 的 pin 完全一致；
3. `corepack pnpm --dir browser-worker install --frozen-lockfile && … build`，并 `test -f
   browser-worker/dist/profile.js`；
4. `corepack pnpm --dir tui install --frozen-lockfile`。

### 2.2 产品 bug：`/proc/cpuinfo` 句柄泄漏（仅 Linux）

```python
for line in open("/proc/cpuinfo", encoding="utf-8"):  # noqa: SIM115
    if line.lower().startswith("model name"):
        return line.partition(":")[2].strip() or None
```

命中型号那一行直接 `return`，文件对象永远不是显式关闭的，CPython 析构时报
`ResourceWarning: unclosed file`。macOS 走 `platform.processor()` 分支，所以只在 Linux 出现。

对用户的影响：默认 warning filter 下 `ResourceWarning` 是静默的，因此不会污染正常 CLI
输出；但描述符要等 GC 才释放，且任何开启了 warning 的运行方式（`-X dev`、测试框架、
把 stderr 当契约的调用方）都会看到噪声。改成 `with open(...)`。

### 2.3 产品 bug：receipt 与 downloads root 的 inode 钉扎

与第一轮同一类。`_unlink_same_inode` / `_quarantine_expected` 只凭 `(st_dev, st_ino)`
判断「这还是不是我创建的东西」。这个判断只有在**调用方持有指向该 inode 的打开描述符**
时才成立：描述符还开着，内核就不会回收 inode 号；一旦提前关闭，Linux 会立刻把它复用给
新建文件/目录，竞态进程在同一 pathname 上的新对象就可能顶着同一个 inode 号被我们删掉。

本轮补上两处：

- `InstallReceipt.write`：描述符原本在 `os.replace` 之前就被内层 `finally` 关掉，之后的
  temp 清理与 `_restore_receipt` 全程无保护。改为在外层 `finally` 关闭——`finally` 在
  `except` 之后执行，清理期间描述符仍然打开。
- `_restore_receipt`：recovery 描述符同理，持有到 link 发布与全部清理结束。
- `InstallOperations._download_roots`：改存 `(root, identity, descriptor)`，在 `download()`
  里用 `O_DIRECTORY|O_NOFOLLOW` 打开 downloads root 并 `os.fstat` 取 identity，事务
  `cleanup()` 删完再关。目录 inode 同样能被描述符钉住。

对应的三个用例原本靠「macOS 不会立刻复用 inode 号」这一巧合来制造 identity 失配。现在
让测试按真实契约持有描述符（或先建 sibling 再 `os.replace`），失配在两个平台上都必然
成立，断言反而更硬：证明的是「identity 校验发生在原子 quarantine/rename 之后，失配时
恢复而不是删除」这条真正的不变量。

### 2.4 产品缺陷：Linux 上 `personal` profile 没有任何写根

`resolve_permission_roots` 把默认写根整段关在 `if platform == "darwin"` 里。Linux 用户
选了 `personal` profile，能读整个 Home，却在 Workspace 之外一处都不能写——而 Linux 是
本项目的头号目标平台。

`Desktop`/`Documents`/`Downloads` 正是 Linux `xdg-user-dirs` 的默认英文名，
`PycharmProjects`/`WebstormProjects` 是 JetBrains 各平台相同的默认目录，且
`_existing_unique_roots` 本来就会剔除不存在的目录。因此把这段提到平台判断之外；
`/Applications`、`/opt/homebrew`、`/usr/local` 这三个**读**根仍然只在 macOS 生效。

### 2.5 测试假设：ambient 解释器不是受信 tree

`test_default_runner_builds_with_offline_fake_uv` 需要一个能真正执行的完整 CPython，
于是直接把 `sys.base_prefix` 当 `managed_python_root`。但那棵树的位置、权限与内容完全
由环境决定，installer 的 trusted-tree 校验会——正确地——拒绝其中好几种形态。改为复制进
测试自己的 0700 sandbox，归一化 mode、丢弃 casefold 重复项、清掉因此悬空的 alias。

## 3. 未修复：真实 uv managed Linux Python 会被安装器拒绝

**这是本轮发现的、比第一轮更严重的产品缺陷，尚未修复。**

`release/install.sh.tmpl` 用 `uv python install 3.12` provision managed Python，并把它的
根目录作为 `--managed-python-root` 交给 installer。`_preflight` 会对这棵树跑
`_validate_source_tree`，其中有一条 casefold 去重：

```python
if relative.casefold() in seen:
    _runtime_failed()
```

而 uv 在 Linux 上装的 CPython（python-build-standalone）带 `share/terminfo`，里面存在
`share/terminfo/P` 与 `share/terminfo/p`、`N` 与 `n` 等仅大小写不同的条目。实测
`cpython-3.12.13-linux-x86_64-gnu` 有 4697 个条目、33 处 casefold 冲突，
`cpython-3.12.13-linux-aarch64-gnu` 有 4915 个条目、33 处冲突。

实测结论（非 root 容器内直接调用产品函数）：

```
managed python root: …/uv/python/cpython-3.12.13-linux-aarch64-gnu
REJECTED: InstallError runtime_install_failed: manifest
```

即：**Linux 上任何一次真实的一行安装都会以 `runtime_install_failed: manifest` 失败。**
macOS 侥幸躲过，因为 python-build-standalone 的 macOS 构建链接系统 ncurses、不带
terminfo，而且大小写不敏感的文件系统上这类条目本来就无法共存。

这条检查本身是有价值的（防止把 tree 复制到大小写不敏感的目标时静默合并条目），所以
**不应该简单删掉**。可选方向：

1. 只在目标文件系统确实大小写不敏感时才拒绝（一次性探测）；
2. 依赖 `_copy_verified_tree` 复制后与源 manifest 的比对来发现真实合并，把这条前置拒绝
   降级为目标侧校验。

两条都会改动安装器的信任策略，需要单独设计与评审，因此本轮只记录、不擅自修改。发布
Linux 版之前必须先解决。

## 4. 验证

- Linux：`docker run python:3.12-slim` + 非 root 用户 + uv managed Python，逐模块红→绿；
  与 `git stash` 后的同容器基线对比，`comm` 显示 4 项由失败转通过、**0 项新增失败**。
  `test_browser_evals` 与 `test_cli_eval` 在补齐 Node 与 `browser-worker/dist` 后通过；
  `TuiBundleTest` 在 `corepack prepare` + `pnpm --dir tui install` 后 5/5 通过。
- macOS：`uv run python -m unittest discover -s tests -q` 全绿。
- `uv run --with ruff ruff check .`、`uv run python scripts/validate_docs.py` 均通过
  （顺带修掉 `3f14383` 引入的一处 `I001` import 排序，它同样会挡住 offline gate）。

## 5. 容器里的环境噪声（不要追）

`python:3.12-slim` 里另有约 43 项 error / 3 项 failure，在改动前后完全一致，且在 GitHub
runner 上不出现。已确认的成因包括：`/usr/bin/python3` 不存在（closed-world PATH 固定为
`/usr/bin:/bin`）、uv 数据目录是 group-writable、以及 `ACTION-OPEN-APP-001` 在无桌面环境
的容器里必然 `execution_error`。判断改动效果只看**增量**。
