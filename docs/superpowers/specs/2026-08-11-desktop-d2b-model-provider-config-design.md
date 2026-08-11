# Desktop D2b：多 Provider 模型配置与密钥写入

> 日期：2026-08-11
> 文档类型：Phase D2b 产品、配置与协议设计
> 状态：`IMPLEMENTED`（2026-08-11）
> 上位规划：[对标 ClawX 能力差距与 Milestone 规划 §5](2026-08-11-desktop-clawx-capability-gap-and-roadmap.md)
> 前置实现：[D2a 定时任务可控](2026-08-11-desktop-d2a-automation-control-design.md)（已实现）

## 1. 目标

用户原话：「配置模型和换模型 和填入 API Key」，并明确要求**多 Provider 并存**（对齐 ClawX 的
Models 页：OpenAI / OpenRouter / MiniMax 同时配置、标记 Default、随时切换）。

D2b 完成后：

1. 设置页出现 **Models** 区，列出所有已配置 Provider（名称、base_url、模型、是否默认、密钥是否已配）；
2. 可以**新增 / 编辑 / 删除** Provider，可以把任意一个**设为默认**；
3. 可以为每个 Provider **填入 API Key**，密钥写进 `secrets.env`（0600），永不回显明文；
4. 切换默认 Provider 或改动配置后，界面明确提示需要重启 Core，并提供一键重启。

## 2. 这是本项目第一次让 Core 支持"修改已有配置"

必须先讲清楚现状，因为它决定了这个 milestone 的真实工作量：

| 能力 | 现状 |
| --- | --- |
| 生成 `config.toml` | 有，但只在**首次安装**时整体渲染（`bootstrap.render_default_config`） |
| **修改已有 `config.toml`** | **完全没有**。没有任何函数做"读出来、改一个字段、写回去" |
| 写 `secrets.env` | 有 `write_fresh_setup()`，但是 **fresh-only 全量写**（目标文件已存在就直接拒绝） |
| **就地更新单个密钥** | **完全没有** |
| Provider 数据结构 | **单条**：`ProviderConfig(base_url, api_key_env, timeout_seconds)`，`AgentConfig.model` 是裸字符串 |

所以 D2b 的三块新工程：**多 Provider 数据结构 + 配置就地更新 + 密钥就地更新**。前两块碰的是
用户的真实配置文件，第三块碰的是密钥——三块都属于"写坏了会让应用起不来或泄密"的范畴，因此
本设计的重心全部放在安全与可回滚上，而不是 UI。

## 3. 数据结构：从单条到列表

### 3.1 新的配置形状

TOML 的数组表（array of tables）天然适合表达这个：

```toml
[agent]
model = "deepseek-v4-pro"          # 保留：当前生效的模型名
provider = "deepseek"              # 新增：引用下面某个 provider 的 id

[[providers]]                      # 新增：数组表，可以有多条
id = "deepseek"
base_url = "https://api.deepseek.com"
api_key_env = "LOBSTER0_MODEL_API_KEY"
timeout_seconds = 120

[[providers]]
id = "openrouter"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "LOBSTER0_PROVIDER_OPENROUTER_KEY"
timeout_seconds = 120
```

`id` 由用户提供，限定 `[a-z0-9_-]{1,32}`——它要参与环境变量名的生成，必须是安全字符集。

### 3.2 向后兼容与迁移

**旧配置必须继续能用**，不能要求用户手改文件。加载逻辑：

1. 若存在 `[[providers]]` → 走新路径，`agent.provider` 指定当前生效项（缺失则取第一条）；
2. 若只有旧的 `[provider]` 单表 → **在内存中**把它转成一条 id 为 `default` 的 Provider，行为与
   现在完全一致，**不自动改写用户的文件**；
3. 两者同时存在 → 拒绝加载并给出明确错误，不猜测用户意图。

只有当用户在界面上真的新增/修改 Provider 时，才第一次把文件升级成数组表形式。这条原则很重要：
**读取路径永不写盘**，避免"打开一次应用配置文件就被悄悄改了"。

### 3.3 每个 Provider 一个密钥变量

密钥环境变量名由 id 推导：`LOBSTER0_PROVIDER_<ID大写>_KEY`，例如 `openrouter` →
`LOBSTER0_PROVIDER_OPENROUTER_KEY`。旧的单 Provider 继续用既有的 `LOBSTER0_MODEL_API_KEY`，
迁移时保留原名，不强制改名（改名会让用户已有的 `.env` 失效）。

## 4. 安全设计（本 milestone 的核心）

### 4.1 配置文件就地更新

新增 `config.update_providers()`，语义严格限定：

- **只允许改 `[[providers]]` 与 `agent.model`/`agent.provider` 这几个字段**，其余段落原样保留；
- 采用「读 → 改内存结构 → 全量重新序列化 → 原子写」，不做字符串级 patch（正则改 TOML 极易写坏）；
- 写入前先用现有 `load_config()` 校验新内容能被正常解析，**校验失败就不落盘**——避免写出一个
  让应用起不来的配置；
- 原子写：临时文件（0600）→ `fsync` → `os.replace`，与 `setup.py` 现有做法一致；
- 写入前把原文件备份为 `config.toml.bak`，覆盖上一次备份即可（不做无限历史）。

**已知取舍**：全量重新序列化会丢掉用户手写的注释与字段顺序。这是有意的选择——保留注释需要
一个 round-trip TOML 库（如 `tomlkit`），而引入新依赖的风险高于丢注释的代价。此事必须在
文档和界面提示里写明，不能让用户在不知情的情况下丢掉注释。

### 4.2 密钥就地更新

新增 `setup.update_secret(paths, name, value)`：

- 复用现有 `validate_secret_value()`：拒绝空值、边缘空白、引号开头、NUL、任何 splitlines 分隔符
  ——这些都会破坏 dotenv 或注入第二个变量；
- `name` 必须匹配 `LOBSTER0_[A-Z0-9_]+` **且在调用方给出的白名单内**；协议层再限定一次，
  绝不接受 Renderer 传来的任意变量名（否则等于开放任意环境变量写入）；
- **保留文件中其他所有变量**：逐行解析，命中同名则替换该行，未命中则追加；不重排、不去重；
- 文件不存在则以 0600 创建；已存在则保持 0600 并原子替换；
- 全程不打印、不返回、不记录密钥值。

### 4.3 密钥绝不回流

- Bridge 响应只返回 `configured: bool`，永不返回密钥值或任何前缀/后缀；
- `providers.list` 的响应里只有 id / base_url / 是否默认 / 密钥是否已配置；
- Renderer 侧输入框 `type="password"`，提交后立即清空本地 state；
- 测试用 sentinel 值扫描所有输出（stdout/stderr/响应/异常信息），沿用 `test_setup.py` 既有手法。

### 4.4 破坏性操作的门禁

- 删除 Provider：若它是当前默认，**拒绝删除**并提示先切换默认，避免留下悬空引用；
- 所有写操作沿用 D2a 的忙碌判定（有回合在跑或待审批时拒绝）；
- 切换默认 Provider / 改 base_url / 改密钥都需要重启 Core 才生效，界面必须明确提示，
  不能让用户以为点了保存就已经切换。

## 5. Bridge 协议

| 类型 | payload | 说明 |
| --- | --- | --- |
| `providers.list` | `{}` | 只读，返回全部 Provider 摘要（不含密钥） |
| `providers.upsert` | `{id, base_url, timeout_seconds, model?}` | 新增或更新一条 |
| `providers.remove` | `{id}` | 删除；默认项拒绝删除 |
| `providers.select` | `{id, model}` | 设为默认并指定模型名 |
| `providers.set_secret` | `{id, value}` | 写该 Provider 的密钥；变量名由 id 推导，不由调用方指定 |

`capabilities` 追加 `"providers_write"`。注意 `set_secret` 的 payload **不含变量名**——由 Core 从
id 推导，这样 Renderer 根本没有指定写哪个环境变量的能力。

## 6. 界面

设置页新增 Models 区（不新建导航页，避免侧栏过长）：

- 每个 Provider 一张卡：id、base_url、模型名、`默认` 徽标、密钥状态（`已配置` / `未配置`）；
- 操作：设为默认、编辑、填写密钥、删除；
- 顶部「新增 Provider」；
- 任何写操作成功后显示「需要重启 Core 生效」并提供重启按钮。

capability 缺失时整个 Models 区退化为只读列表，与 D2a 的做法一致。

## 7. TDD 起点

Python：

- 加载：新数组表、旧单表、两者并存（拒绝）、id 非法字符、重复 id、`agent.provider` 指向不存在的 id；
- 配置就地更新：其他段落原样保留、写入前校验失败则不落盘、原子性（中断不留半文件）、备份生成；
- 密钥就地更新：保留其他变量、同名替换、不存在则追加、非法值拒绝、权限保持 0600；
- 密钥不出现在任何输出中（sentinel 扫描）；
- protocol：五个新请求类型的 exact-key、id 字符集、删除默认项被拒。

Desktop：IPC 校验、密钥框不回显、capability 缺失时只读、删除默认项的错误提示。

## 8. 退出条件

1. 旧 `config.toml` 不改一个字也能正常加载运行；
2. 能新增第二个 Provider、填密钥、设为默认，重启后真实生效；
3. `secrets.env` 中其他变量不受影响，权限仍是 0600；
4. 密钥明文不出现在界面、日志、Turn 记录、Bridge 响应中（有测试证明）；
5. 写入失败或校验不通过时不留下损坏的配置文件；
6. Python + Desktop 全量门禁通过，真实 Electron + Bridge smoke 覆盖一次"新增 Provider → 设默认 → 重启"。

## 8.1 落地记录（2026-08-11）

按设计实现，与文档的差异只有一处：**API Key 输入框放在 Provider 卡片主层**，而不是
藏在「编辑」折叠里。填密钥是这页最主要的动作，折叠一层等于给最常用的操作加了一道门。

安全边界最终落在**两道独立关卡**上，Main 层与 Core 协议层各拒绝一次：

- `providers.upsert` 两层都拒绝调用方传 `api_key_env`；
- `providers.set_secret` 的 payload 只有 `{id, value}`，变量名由 `config.provider_secret_env()`
  从 id 推导——Renderer 在结构上就没有指定写哪个环境变量的能力；
- 密钥值不做 trim 后转发（必须逐字节保真），只判定合法性；
- 写失败时只回固定文案，不透传异常文本（可能带上路径或值）。

`StatePaths` 与 `AppConfig` 随 `AgentRuntime` 一起下发给 Bridge，这是 Provider 写操作
需要的唯一入口。

退出条件全部覆盖。第 6 条用真 Bridge 子进程的端到端测试兑现（起进程 → 新增 Provider →
设为默认 → 读回 `config.toml`），它抓到了一个单测抓不到的缺陷：

**`_run_provider_action` 原本用 Runtime 启动时的配置快照**，于是"新增 Provider"紧接着
"设为默认"必然失败——第二步看不见第一步刚写进磁盘的条目。改为每次从磁盘重读。
这类只在连续写操作之间暴露的问题，单测的形状断言无论如何都测不出来。

## 9. 明确不做

- **请求级的自动路由 / fallback / 负载均衡**——本次只做配置层的并存与手动切换。落地文档
  「不为未来多模型建设路由平台」的约束在这一点上仍然有效；
- 从 Provider 拉取可用模型列表（`ModelProvider` 协议没有 `list_models`，各家接口也不统一）；
  模型名仍由用户手填；
- 保留 TOML 注释（见 §4.1 的取舍说明）；
- Channel 凭据的界面配置——那是另一条安全边界，不在本 milestone。
