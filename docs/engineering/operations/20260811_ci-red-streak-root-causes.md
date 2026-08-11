# CI 连续全红的四个根因与修复

> 日期：2026-08-11 · 范围：`.github/workflows/{ci,release}.yml`、`scripts/normalize_sdist.py`、
> `tests/test_install_{runtime,platforms,matrix_contract}.py`、`tests/test_release_bundles.py`

## 1. 背景

`gh run list --workflow ci.yml --limit 60` 显示最近 60 次运行里 **没有一次 success**
（44 failure、15 cancelled）。表面上像"CI 一直在报错"是同一个毛病，实际是四个互相独立的
故障叠在一起，而且彼此遮挡：靠后的关卡从没跑到过，所以它们的问题一直没被看见。

所有结论以 run `31504526816` 的原始日志为准，并在本地干净 clone 上复现后才动手。

## 2. 四个根因

| # | 失败 job / 步骤 | 根因 | 性质 |
| --- | --- | --- | --- |
| 1 | offline gate → 单元测试（135 errors） | `ReleaseManifest` 的 `database_schema` 绑定 `_DATABASE_SCHEMA_VERSION`（已随 migration 0011/0012 升到 12），两个测试 fixture 仍写死 `10` | 测试 |
| 2 | node 24 pi-tui → 构建 bundle | job 从不安装 Python 包，却用裸 `python3` 调用顶层 `import lobster0` 的脚本 | CI 配置 |
| 3 | artifact reproducibility → sdist 双构建比对 | setuptools 的 sdist 无视 `SOURCE_DATE_EPOCH`，按 PAX 把构建时刻写进每个成员 | 构建 |
| 4 | offline gate → Ruff（尚未暴露） | `main` 上已有 6 处 lint 错误，被 #1 挡在后面从没跑到 | 代码卫生 |

### 2.1 fixture 与 migration 源头脱节

135 个 error 的 traceback 完全一致，全部来自 `InstallRuntimeTests.setUp` 构造 manifest：

```
lobster0.install.models.InstallError: manifest_invalid: database_schema
```

`src/lobster0/install/models.py:420` 要求 `database_schema == _DATABASE_SCHEMA_VERSION`，
该常量已经是 12；而 `tests/test_install_runtime.py` 与 `tests/test_install_platforms.py`
的 fixture 还写着 `10`。`tests/install/manifest_v1.json` 当时已同步到 12，说明升级
migration 时改了 JSON fixture，漏了两处 Python fixture。

处置不是把 `10` 改成 `12`，而是改为引用 `LATEST_SCHEMA_VERSION`——`test_release_manifest_build`
里已有 `test_database_schema_tracks_the_migration_source_of_truth` 把常量钉在 migrations
目录上，fixture 一并挂到同一个源头，下次新增 migration 就不会再漏。

### 2.2 bundle 脚本的 import 前提在该 job 不成立

`scripts/build_tui_bundle.py:18` 顶层 `from lobster0.tui_launcher import is_supported_node_version`。
`ci.yml` 里两类调用方式的差别正是问题所在：

- `uv run python scripts/...`（validate_docs、build_installer_zipapp）——有已同步的环境；
- `python3 scripts/...`（build_node_bundle、build_tui_bundle）——没有。

`build_node_bundle.py` 不 import `lobster0`，所以它一直是过的；`build_tui_bundle.py` 必然
`ModuleNotFoundError`。`release.yml` 的同一处调用有相同缺陷，一并修——否则这个坑会在真实
发布时才爆。

补上 `PYTHONPATH=src` 之后，run `31507064314` 露出了**第二层**：这次 import 找得到
`lobster0` 了，却栈到 `lobster0.tui_launcher` → `lobster0.tui` → `lobster0.tui.app` →
`import rich`，而该 job 同样不安装第三方依赖。

`build_tui_bundle.py` 需要的只是 `is_supported_node_version` 这一个纯版本比较函数，却因为
`tui_launcher` 在**模块顶层** `from lobster0.tui import run_tui` 而被迫拖进整个 Textual 栈。
处置不是给这个 job 装依赖——为一个纯函数装 rich/textual 既慢又本末倒置——而是把这条 import
下沉到 `run_default_tui` 函数内部：4 处 `run_tui` 调用全在该函数里，且都是 **Textual 回退
路径**。pi-tui 才是默认路径，走默认路径的人本来就不该为 Textual 付出 import 成本，所以这
同时是一处架构上的收敛，而不只是给 CI 让路。

代价是 `tests/test_tui_launcher.py` 里 3 处 `mock.patch("lobster0.tui_launcher.run_tui")`
必须改指向 `lobster0.tui.run_tui`：函数内 from-import 在调用时才读模块属性，patch 打在真实
模块上依然生效，但打在 launcher 命名空间上就不再有对象可替换。

验证用的是一个**不装任何第三方包**的 3.12 venv（等价于该 job 的条件）：
`PYTHONPATH=src python -c "from lobster0.tui_launcher import is_supported_node_version"`
成功，`build_tui_bundle.py --help` 与 `normalize_sdist.py --help` 均退出 0。

### 2.3 setuptools 的 sdist 无法逐字节复现

门禁连续两次 `uv build` 后比对摘要，wheel 一致、sdist 每次不同。逐层剥开：

- 解包后 `diff -r` 与文件清单 **完全一致**，差异不在内容；
- `tar -tvf` 的权限/uid/size/mtime/顺序也一致；
- 二进制比对定位到第一处差异在 offset 531，即第一个成员的 PAX 扩展头：

```
28 mtime=1786460737.8325148     ← 第一次构建
28 mtime=1786460739.5208719     ← 第二次构建，相差 2 秒
```

实测确认这与 `SOURCE_DATE_EPOCH` 无关：显式传 `SOURCE_DATE_EPOCH=1000000000`（2001 年），
产出的顶层目录 mtime 仍是构建当下。也就是说，**这道门禁对 sdist 从设计上就不可能通过**，
它不是被某次改动弄坏的。

#### 为什么选择归一化而不是缩小门禁

两个方向：把 sdist 改成只验"内容可复现"，或者让 sdist 真正可复现。选后者，因为
`release.yml` 会把 sdist 作为正式产物上传并记录 `sdist_sha256`——如果只验内容，对外发布的
sdist 就永远无法从源码树重建出同一字节。

新增 `scripts/normalize_sdist.py`，复用 `build_node_bundle.write_deterministic_tar`，把 sdist
重写为与 Node/TUI bundle 同一套规则：USTAR 格式（不带 PAX 高精度 mtime）、成员按名排序、
mtime 与 uid/gid 归零、uname/gname 清空、gzip mtime 归零、原子替换。解包走 `filter="data"`，
并拒绝非 regular 成员、逃逸路径、多顶层目录，同时设成员数与总字节预算。

关键是 CI 门禁与 `release.yml` 调用的是**同一个脚本**，所以"证明可复现的那份"和"发布出去
的那份"是同一构造，而不是只在门禁里做一次性比较。两条契约测试锁住这一点：
`test_artifact_build_normalizes_the_sdist_before_comparing` 与
`test_published_sdist_is_normalized_before_its_digest_is_taken`（后者还断言归一化排在取摘要之前）。

### 2.4 被遮挡的 Ruff 关

Ruff 在 offline gate 里排在单元测试之后，而单测从没跑通，所以 `main` 上积累的 6 处错误
（5 处 I001 import 顺序、1 处 E501）一直没暴露。修完 #1 之后它们会立刻变成新的红。

## 3. 验证

| 项 | 结果 |
| --- | --- |
| `test_install_runtime` + `test_install_platforms` + `test_install_models` + `test_release_manifest_build` | 184 passed（Python 3.12） |
| `manifest_invalid` 计数 | 135 → 0 |
| `test_install_matrix_contract` + `test_release_bundles` | 87 passed |
| sdist 归一化前 | 两次构建摘要不同 |
| sdist 归一化后 | 两次构建摘要相同，且 gzip mtime 字段为 `\0\0\0\0` |
| 归一化产物无损性 | 解包后 `diff -r` 与文件清单与原始 sdist 完全一致 |
| 归一化产物可安装性 | `uv pip install` 后 `import lobster0` 可用，`schema.sql` 与 11 个 migration 的 package-data 齐全 |
| `ruff check .` | All checks passed |
| `scripts/validate_docs.py` | PASS |
| `test_tui_launcher` + `test_cli` + `test_doctor` + `test_release_bundles` | 70 passed |
| 空 3.12 venv（无 rich）import `tui_launcher` | 成功，`is_supported_node_version` 行为不变 |

run `31507064314`（修复第 1～3 条之后）的实测结果：offline gate、artifact
reproducibility、node 22 pi-tui 全部 success——包括此前从没跑到过的 Ruff、文档契约、
分发构建，以及 artifact-build 尾部的 installer 对抗与离线安装矩阵。仅剩 node 24 栽在
上面 2.2 的第二层，即本节最后一处修复。

**最终结果**：补上该修复后，run `31507975543`（commit `76846e1`）四个 job——offline
gate、artifact reproducibility、node 22 pi-tui、node 24 pi-tui——**全部 success**。这是
此前 60 次运行（44 failure、15 cancelled、0 success）之后的第一次全绿。

本地跑这套测试必须用 Python 3.12：`test_install_runtime` 需要一个名为 `python3.12` 的
managed interpreter，在 3.13 上会有 82 个与本次修复无关的 `FileNotFoundError`。

## 4. 遗留

修复 #4 时，`src/lobster0/bridge/server.py`、`tests/test_bridge_protocol.py`、
`tests/test_bridge_server.py` 三个文件里同时存在另一个会话正在写的功能代码（artifacts
的 IPC 接线）。为不把他人的半成品推上去，这三处的 lint 修复留在了工作树里，由该会话
自己的提交携带；`turn.py`、`runtime.py`、`test_turn.py` 三处纯 import 重排已单独提交。
