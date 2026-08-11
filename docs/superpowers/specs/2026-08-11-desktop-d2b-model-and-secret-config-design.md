# Desktop D2b：多 Provider 模型配置与密钥写入

> 日期：2026-08-11
> 文档类型：Milestone D2b 设计
> 状态：`DRAFT FOR REVIEW / IMPLEMENTATION PENDING`
> 上位规划：[对标 ClawX 能力差距与 Milestone 规划](2026-08-11-desktop-clawx-capability-gap-and-roadmap.md)

## 1. 范围确认

上位规划建议「先做单 Provider 可编辑，多 Provider 留到以后」，用户明确要**多 Provider**。
本文档按多 Provider 设计，并如实标注这带来的额外工作量与风险。

## 2. 两个决定性的现状事实（已查证）

### 2.1 配置从来是只读的

全仓搜索没有任何 `save_config` / `write_config` / `update_config`——`config.toml` 自项目建立以来
**只被读取，从未被程序写回**。所有配置变更都靠用户手改文件或 `setup` 时一次性生成。

D2b 是**第一次引入配置写入能力**。这不是「加个接口」，而是要新建一套「安全地改配置文件」的机制，
并回答一系列此前不存在的问题：并发写、写坏了怎么恢复、写入后运行中的 Runtime 怎么办。

### 2.2 现有的私密写入函数不能直接复用

`setup.py:_write_private_file()` 用 `os.O_EXCL`，**目标文件已存在就抛错**——它是为 fresh setup 设计的，
天生拒绝覆盖。D2b 需要的是"更新一个已存在的文件"，必须新写函数，不能套用。

可复用的是它的**安全要素**（这些要照搬）：0600 权限、`fchmod`、`fsync`、失败时清理。
外加 `validate_secret_value()` 的值校验（拒绝空值/边缘空白/引号开头/NUL/任何换行符）。

### 2.3 当前数据结构是单数

```python
class ProviderConfig:           # 一组，不是列表
    base_url: str
    api_key_env: str            # 只存变量名，不存密钥值
    timeout_seconds: int

class AgentConfig:
    model: str                  # 单个字符串，如 "deepseek-v4-pro"
```

改成多 Provider 意味着 `config.py` 的数据结构、`runtime.py` 的构造、`bridge/server.py` 的
`client.hello` 响应都要跟着改，还要处理**既有配置的迁移**。

## 3. 数据结构设计

### 3.1 新的配置形态

```toml
# 既有单数写法继续被接受（见 §3.2 迁移）
[provider]
base_url = "https://api.deepseek.com"
api_key_env = "LOBSTER0_MODEL_API_KEY"
timeout_seconds = 120

# 新增：多 Provider
[[providers]]
id = "deepseek"                              # 稳定标识，用于引用
label = "DeepSeek"
base_url = "https://api.deepseek.com"
api_key_env = "LOBSTER0_PROVIDER_DEEPSEEK_KEY"
timeout_seconds = 120

[[providers]]
id = "openrouter"
label = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "LOBSTER0_PROVIDER_OPENROUTER_KEY"

[agent]
model = "deepseek-v4-pro"
provider = "deepseek"                        # 新增：指向 providers[].id
```

- `id` 限定 `[a-z0-9_-]{1,32}`，是环境变量名的组成部分，必须严格约束；
- `api_key_env` 由 `id` 推导（`LOBSTER0_PROVIDER_<ID大写>_KEY`），**不接受用户自定义**——
  否则用户可以指定任意环境变量名，等于任意读取进程环境。

### 3.2 迁移：不破坏既有配置

现存配置只有 `[provider]` 单数段。规则：

1. 只有 `[provider]`、没有 `[[providers]]` → 视为一个 id 为 `default` 的 Provider，行为完全不变；
2. 两者同时存在 → **拒绝加载并报明确错误**，要求用户二选一（静默偏向任一方都会让人搞不清实际生效的是哪个）；
3. 只有 `[[providers]]` → 新形态，`agent.provider` 必填且必须命中某个 id。

规则 1 保证**现有用户不需要做任何事**；规则 2 保证不会出现"我改了这里但生效的是那里"。

## 4. 配置写入机制（本 milestone 的核心）

### 4.1 新增 `config_writer.py`

不在 `config.py` 里加写入——那个模块的职责是「解析并校验只读输入」，混入写入会让它同时是真相的
读者和作者。新建独立模块，职责单一。

核心函数：

```python
def update_config_atomically(paths, mutate: Callable[[dict], dict]) -> None
```

流程：读原文 → 解析成可变结构 → 交给 `mutate` 修改 → 序列化 → **用 `load_config` 重新校验一遍** →
原子替换。

**写入前必须用既有的 `load_config` 校验产物**——这是防止写出一个让 Core 起不来的配置的唯一可靠手段。
校验不通过就整体放弃，原文件保持不动。

### 4.2 原子替换与并发

- 写临时文件（同目录、0600）→ `fsync` → `os.replace()`（同分区原子）→ `fsync` 父目录；
- 替换前对比原文件的 `st_mtime_ns` + `st_size`，与读取时不一致则中止并要求重试——防止覆盖掉
  另一个进程（比如用户手动编辑、或另一个 Gateway）刚写入的内容；
- 写入前自动备份为 `config.toml.bak-<timestamp>`（本次会话已在手工调整上限时验证过这个做法有效）。

### 4.3 密钥写入：`secrets.env` 的就地更新

新增函数，与配置写入同样的原子策略，外加：

- 值必须通过 `validate_secret_value()`；
- **只替换目标变量所在的那一行，其余行原样保留**（现有 `write_fresh_setup` 是全量重写，不适用）；
- 变量名必须在**白名单**内：模型/Provider 密钥（`LOBSTER0_PROVIDER_*_KEY`，且 `*` 必须命中已存在的
  Provider id）、各 Channel 已定义的凭据名。不接受任意变量名——否则 Renderer 就能写任意环境变量；
- 文件权限强制 0600，写完复核。

## 5. Bridge 协议变更

| 类型 | payload | 说明 |
| --- | --- | --- |
| `providers.list` | `{}` | 返回各 Provider 的 id/label/base_url/**是否已配置密钥**（布尔，不返回密钥） |
| `providers.upsert` | `{"id", "label", "baseUrl", "timeoutSeconds"}` | 新增或更新，不含密钥 |
| `providers.remove` | `{"id"}` | 删除；正被 `agent.provider` 引用时拒绝 |
| `agent.model.set` | `{"model", "providerId"}` | 切换当前模型与 Provider |
| `secret.set` | `{"name", "value"}` | 就地更新 `secrets.env`；name 走白名单 |
| `bridge.restart` | `{}` | 复用 `bridge-service.ts` 已有重启链路 |

capabilities 追加 `"config_write"`。

**`secret.set` 的响应绝不回显值**，只返回成功与否。`providers.list` 用 `hasKey: bool` 表达配置状态。

## 6. 生效方式：必须重启

改完配置后运行中的 `AgentRuntime` 仍持有旧 provider 实例。选项：

- **热重载**：要在 runtime 里做 provider 替换，涉及正在跑的回合、连接池、飞书 Gateway 的长连接——
  复杂且容易留下半新半旧状态；
- **重启 Bridge**（采用）：简单、状态干净，桌面端已有完整的重启链路（`restartWorkspace` 同款）。

界面在保存后明确提示「需要重启才能生效」并提供一键重启；**有回合在跑时禁止重启**，沿用 `taskBusy`。

飞书 Gateway 是独立进程，不受桌面端重启影响——文档需要写明这点，避免用户以为改了配置飞书那边也会跟着变。

## 7. 安全门禁汇总

| 风险 | 门禁 |
| --- | --- |
| Renderer 写任意环境变量 | `secret.set` 的 name 白名单 |
| 密钥泄漏到日志/响应/UI | 值不进日志、不回传、界面 `type="password"` 且不回显；用 sentinel 扫描输出的测试 |
| 写出起不来的配置 | 写入前用 `load_config` 全量校验，不通过则放弃 |
| 覆盖他人并发写入 | mtime+size 前置比对 |
| 写坏无法恢复 | 自动备份 + 原子替换 |
| 删除正在使用的 Provider | `providers.remove` 检查引用 |
| 恶意 Provider id | `[a-z0-9_-]{1,32}` 严格约束（它参与构造环境变量名） |
| base_url 指向内网 | 必须 https（本地回环放行），复用既有 SSRF 校验思路 |

## 8. TDD 起点

**配置写入**：mutate 产物非法时原文件不变；mtime 变化时中止；备份确实生成；权限 0600；
临时文件在失败后不残留。

**迁移**：只有 `[provider]` 时行为与今天完全一致（回归保护）；两者并存时拒绝加载且错误信息可操作；
`agent.provider` 指向不存在的 id 时拒绝。

**密钥**：只更新目标行、其他变量原样保留；值含换行/引号/NUL 被拒；白名单外的 name 被拒；
sentinel 值不出现在任何日志、响应、异常里。

**Provider 管理**：删除被引用的 Provider 被拒；id 格式非法被拒；重复 id 被拒。

**Desktop**：密钥框不回显、已配置只显示布尔状态；保存后提示重启；busy 时禁止重启。

## 9. 退出条件

1. 既有单 `[provider]` 配置零改动继续工作（回归测试证明）；
2. 能新增第二个 Provider、填入其密钥、切换 `agent.provider` 并重启后真实生效；
3. 密钥明文不出现在界面、日志、Turn 记录、Bridge 响应中（有测试证明）；
4. 写入失败/校验不通过时原配置完好，且有明确错误提示；
5. 删除被引用的 Provider 被拒绝；
6. Python + Desktop 全量门禁通过；真实 smoke 覆盖一次「加 Provider → 填密钥 → 切换 → 重启 → 生效」。

## 10. 明确不做

- 模型能力探测（DeepSeek 的 `list_models`）——Provider 协议里没有，不为此改协议；
- 热重载 provider（§6）；
- 在桌面端管理飞书 Gateway 的配置（独立进程，另议）；
- 从界面配置 `max_tool_iterations` 等运行参数（本次只覆盖模型与密钥；这些参数用户已能直接改配置文件）。

## 11. 工作量与风险提示

这是三个 milestone 里**风险最高**的一个，因为它同时引入了：配置写入（此前不存在）、数据结构变更
（单数→复数）、密钥写入（安全敏感）、配置迁移（不能破坏既有用户）。

D2a 是纯接线，D2c 复用既有 ArtifactStore，只有 D2b 触及"给 Core 加一种它从来没有过的能力"。
建议实施顺序为 **D2a → D2b → D2c**，让 D2a 先把 Bridge 写操作的模式跑通，D2b 再在成熟模式上做更难的部分。
