# MiniClaw → Lobster0 全产品改名设计

**日期：** 2026-08-10
**状态：** 用户已确认，执行中
**工作区：** 隔离 worktree `.worktrees/rename-lobster0`，分支 `codex/rename-lobster0`

## 为什么要改

用户把项目的 GitHub 仓库从 `NEDONION/mini-claw` 改名为 `NEDONION/lobster0`（已核实 repo ID `1326251311` 完全一致，是真的改名，不是新建仓库）。随后用户明确要求把"安装包"也改成这个名字。考虑到这个代码库的安全/身份模型把 GitHub 仓库身份、PyPI 分发名、CLI 命令、Python import 名和本地状态目录紧密绑定在一起（见 `docs/superpowers/specs/2026-08-09-one-line-install-and-release-design.md`），只改 URL 会让产品内部自相矛盾——CLI 还叫 `miniclaw`，状态目录还是 `~/.miniclaw`，但仓库和品牌已经是 `lobster0`。当面确认后，用户明确要求：全部都改成 `lobster0`。

这次和之前 PR #4 修的"仓库地址不一致"（`NEDONION/miniclaw` 拼写错误 → `NEDONION/mini-claw`，产品身份本身没变）性质完全不同。这次是用户主动要求的产品身份/品牌变更。

## 为什么现在可以直接整体改名

- **还没有对外发布过任何东西。** `src/miniclaw/_version.py` 还在 `0.7.0` 之前，没有打过 `v0.7.0` tag，PyPI/GHCR/GitHub Release 都还没发布过任何东西。也就是没有真实用户已经有 `~/.miniclaw` 状态目录或者装了 `miniclaw` 这个 CLI 需要迁移或者兼容——这比"改一个已经发布出去的产品"简单得多，可以直接做纯粹的标识符替换，不需要任何迁移/兼容层。
- Git 提交历史（已有的 commit message、PR 标题、ledger 里以前的记录）**不改**——这是不可变的历史记录，不管品牌怎么变都不应该重写，这是标准做法。

## 改名范围

全仓库扫描确认（大小写不敏感统计 "miniclaw"：515 个文件，约 6271 处）：

| 层面 | 改前 | 改后 |
| --- | --- | --- |
| Python 包/import | `src/miniclaw/`（目录）、`import miniclaw`、`from miniclaw...` | `src/lobster0/`、`import lobster0`、`from lobster0...` |
| CLI 命令 | `miniclaw`（`pyproject.toml` 的 `[project.scripts]`） | `lobster0` |
| PyPI 分发名 | `miniclaw-agent` | `lobster0-agent`（沿用现有 `<名字>` → `<名字>-agent` 的命名习惯；2026-08-10 确认 PyPI 上可用，光秃秃的 `lobster` 已被占用） |
| 状态目录 | `~/.miniclaw` | `~/.lobster0` |
| 环境变量前缀 | `MINICLAW_*`（38 个不同变量：`MINICLAW_HOME`、`MINICLAW_PREFIX`、`MINICLAW_MODEL_*`、`MINICLAW_TEST_*` 等） | `LOBSTER0_*`，后缀不变 |
| GitHub 仓库引用 | `github.com/NEDONION/mini-claw`（PR #4 已经从错误的 `miniclaw` 改对过一次） | `github.com/NEDONION/lobster0` |
| npm 包 scope | `@miniclaw/pi-tui`、`@miniclaw/desktop`、`miniclaw-website` | `@lobster0/pi-tui`、`@lobster0/desktop`、`lobster0-website` |
| `docs/superpowers/` 下的设计/计划文档 | 正文写"MiniClaw" | 改成"Lobster0"，保持一致——这些是仍在被 ledger 和 PR 描述引用的活文档，不是像 git commit message 那样冻结的历史记录 |
| 官网内容、README、README_EN | "MiniClaw" 品牌 | "Lobster0" 品牌 |
| 测试文件（`tests/*.py`，约 300 个） | `import miniclaw` / 引用 `miniclaw` 路径的 fixture | 对应改成新名字 |

**明确不改的部分：**
- Git commit message 和 PR 历史记录（已合并的 PR #1-#5）——原样保留，不可变历史。
- 我（助手）在仓库外维护的 `project_miniclaw_install_milestones` 之类的记忆文件——单独更新，不算这次代码改动的一部分。
- `.git/` 目录下的任何内容。

## 验证方式

这次改名有个天然的自我验证机制：因为几乎所有约 300 个测试文件开头都有 `import miniclaw`（或 `from miniclaw...`），只要改名不彻底或者不一致，跑全量测试套件时会立刻报出 import/collection 错误。执行计划：

1. `git mv src/miniclaw src/lobster0`，同步改 `pyproject.toml`（`name`、`[project.scripts]`、以及其他硬编码包名的地方）和 `[tool.setuptools.packages.find]`（如果里面写死了包名）。
2. 写一个保留大小写变体的全仓库批量替换脚本（`miniclaw`→`lobster0`、`MiniClaw`→`Lobster0`、`MINICLAW`→`LOBSTER0`、`Miniclaw`→`Lobster0`），覆盖 source、tests、scripts、docs 和官网内容，排除 `.git/`。
3. 改 npm 包的 `name` 字段（`@miniclaw/pi-tui` → `@lobster0/pi-tui` 等）——注意这些是工作区内部未发布的包名，不涉及真实 npm registry，不需要改 lockfile 里的 hash。
4. 跑全量 Python 测试、Ruff、docs validator，以及 TUI/Desktop/Browser 各自的测试+构建门禁。把机械替换漏掉的引用逐一补上，直到除了那个已知的、与本次改名无关的本机 `TuiBundleTest.setUpClass`（Node/pnpm 版本低于门槛）失败之外，其余全部通过。
5. 独立复核（第二遍）重新跑一次所有门禁，并且用全仓库 `grep -i miniclaw` 抽查有没有漏改的地方——除了 `.git/` 和不可变的历史 commit message，应该零命中。

## Ledger

记录在这个 worktree 里的 `.superpowers/sdd/2026-08-10-lobster0-rename/progress.md`（本地，不入库，沿用这个仓库对 `.superpowers/sdd/` 目录的既有约定）。
