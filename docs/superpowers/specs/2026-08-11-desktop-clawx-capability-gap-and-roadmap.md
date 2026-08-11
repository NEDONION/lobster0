# Desktop 对标 ClawX：能力差距盘点与 Milestone 规划

> 日期：2026-08-11
> 文档类型：能力差距分析与路线规划
> 状态：`DRAFT FOR REVIEW / IMPLEMENTATION PENDING`
> 对标对象：[ClawX](https://github.com/ValueCell-ai/ClawX)（MIT，同类"给 AI Agent 做桌面壳"产品）
> 关联：[D1～D5 分 Phase 落地](../../engineering/desktop/20260810_桌面多Agent分Phase落地.md)、
> [D2 Composer 控件设计](2026-08-10-desktop-d2-composer-controls-design.md)

## 1. 为什么要重排路线

原 D1～D5 路线是「打开即聊 → 附件/选择器 → Artifact → 多 Agent → 加固」，重心在**对话体验**。
用户实际使用后提出对标 ClawX，并点名两项：**定时任务**、**配置模型/换模型/填 API Key**。

这两项都不在 D2～D5 的既定范围里，但它们的共同特征是：**Core 早就有能力，只是桌面端没有入口**。
继续按原路线做 D2（附件）会跳过这些更基础的缺口——用户连模型都换不了、定时任务只能看不能动。

因此本文档先做一次诚实的能力盘点，再据此重排优先级。

## 2. 能力盘点（逐项查证，不是估计）

### 2.1 ClawX 有而我们没有的功能页

ClawX 侧栏：`New Chat` / **Models** / **Agents** / **Channels** / **Skills** / **Cron Tasks** / `Settings`
我们侧栏：`新建对话` / `对话` / `自动化` / `设置`

| 功能页 | ClawX 的形态（实拍截图确认） | 我们的 Core 能力 | 我们的桌面端现状 | 差距性质 |
| --- | --- | --- | --- | --- |
| **Cron Tasks** | 统计卡（Total/Active/Paused/Failed）+ 任务卡（启停开关、`Daily at 3:00`、上次运行、绑定 Agent）+ New Task | **完整**：`create`/`update`/`pause`/`resume`/`run`/`cancel`/`runs`/`halt`/`unhalt`（`automation/repository.py`、`cli.py` 的 `task` 子命令） | **只读列表**，只能看名称/类型/状态/下次运行时间 | **纯接线**：Core 全有，Bridge 只开了 `automation.list` |
| **Models** | 多 Provider 列表（OpenAI / OpenRouter / MiniMax 并存）、标记 Default、Add Provider、显示鉴权方式与当前模型 | **只有单个**：`ProviderConfig` 一组 `base_url`/`api_key_env`；`AgentConfig.model` 是单个字符串 | 设置页只读显示当前模型名 | **需要后端新建**：多 Provider 是数据结构变更，不是 UI 工程 |
| **Channels** | 多账号配置、按账号绑定 Agent、切换默认账号 | 部分：`config.toml` 有 `[channels.feishu]`/`[channels.discord]`，但都是**文件配置**，无运行时增删改接口 | 无入口 | 需要新建配置写入通道 |
| **Skills** | 本地 Skill 管理、从多源发现、内置文档处理 Skill | 有 `skills/` 模块与惰性正文加载 | 无入口 | 需先确认 Core 的 Skill 增删改边界 |
| **Agents** | Agent 列表、按账号绑定 | **不存在**：`AgentRuntime` 就是单个 provider+tool 集合，无 agent_id、无注册表 | 无入口 | 属 D4 范围，依赖 Phase 9 子 Agent 后端 |

### 2.2 两个关键的后端事实（决定工作量与顺序）

**事实一：模型配置是单数，不是复数。**

```python
class ProviderConfig:          # 一组，不是列表
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "LOBSTER0_MODEL_API_KEY"
    timeout_seconds: int = 120
```

`providers/base.py` 的 `ModelProvider` 协议只有 `complete()`/`aclose()`，**没有 `list_models`**。
所以「换模型」不是接个下拉框：要么先在 Core 引入多 Provider 数据结构（大改动），要么先只做
「改当前模型名 + 改 base_url + 改 API Key」这一档（小改动，覆盖用户 90% 的实际诉求）。

**事实二：配置文件从不保存密钥值，只保存环境变量名。**

`api_key_env = "LOBSTER0_MODEL_API_KEY"`，真实密钥在 `~/.lobster0/secrets.env` 或项目 `.env`。
这个设计是对的（配置文件可以随便看、随便备份，不会泄密），但意味着「在界面上填 API Key」需要
一条**新的、安全的写入通道**——不能简单写进 `config.toml`。已有可复用的先例：`setup.py` 的
`write_fresh_setup()` 用 0600 权限原子写 `secrets.env`，并有 `validate_secret_value()` 拒绝会破坏
dotenv 格式的值。

## 3. 重排后的路线

原则不变（沿用落地文档的五条）：先文档后代码、每个 Phase 交付可独立运行的真实纵切、优先复用现有
Core、后端没有的能力不做假 UI、门禁通过才进下一阶段。

调整：**把「Core 已有能力但桌面端没入口」的部分提到前面**，因为它们投入产出比最高且风险最低；
把需要新建后端数据结构的部分排在后面。

| Milestone | 目标 | 依赖 | 性质 |
| --- | --- | --- | --- |
| **D2a** | 定时任务从只读变可控 | 无（Core 全有） | 接线 |
| **D2b** | 模型与密钥可在界面配置 | 无（复用 setup 的安全写入） | 小幅后端 + UI |
| **D2c** | 原 D2：附件、Composer 控件 | 依赖 ArtifactStore（已有） | 原计划 |
| **D3** | Artifact 与共享产物 | 依赖 D2c | 原计划 |
| **D4** | 多 Agent | 依赖 Phase 9 子 Agent 后端（未完成） | 阻塞中 |
| **D5** | 加固与发布 | 依赖前述 | 原计划 |

Channels 与 Skills 两个功能页暂不排期：前者需要先定义「运行时改 Channel 配置」的安全边界（涉及
凭据与重启），后者需要先确认 Core 的 Skill 增删改能力到底到哪一步。两者都要各自的调研，不适合
现在拍脑袋写进 milestone。

---

## 4. D2a：定时任务从只读变可控

### 4.1 用户结果

- 自动化页显示统计（总数 / 运行中 / 已暂停 / 失败），与 ClawX 的四张卡对齐；
- 每个任务可以**暂停 / 恢复 / 立即运行一次 / 取消**；
- 可以查看某个任务的**历史运行记录**（成功/失败/耗时）；
- 全局 E-stop（急停）可在界面启用与解除，并显示当前是否处于急停；
- 所有操作都走 Core 既有的 durable ledger，不在前端造状态。

~~明确不做：在界面上新建定时任务。~~ **用户 2026-08-11 拍板要做**，已实现表单式新建，见 §4.3。

### 4.2 Bridge 协议变更

`bridge/protocol.py` 的 `_REQUEST_TYPES` 追加（每个都要配 `_validate_payload` 分支）：

| 类型 | payload | 对应 Core 能力 |
| --- | --- | --- |
| `automation.pause` | `{"taskId": int}` | `repository.pause()` |
| `automation.resume` | `{"taskId": int}` | `repository.resume()` |
| `automation.run` | `{"taskId": int}` | CLI `task run` 的同一路径 |
| `automation.cancel` | `{"taskId": int}` | CLI `task cancel` |
| `automation.runs` | `{"taskId": int, "limit": int}` | CLI `task runs` |
| `automation.halt` | `{"reason": str}` | CLI `task halt` |
| `automation.unhalt` | `{}` | CLI `task unhalt` |

`client.hello` 的 capabilities 追加 `"automation_write"`，Desktop 据此决定是否渲染这些控件——
capability 缺失时只显示只读列表，不显示假按钮（沿用「后端没有就不显示」的既定原则）。

### 4.3 界面新建定时任务（用户拍板：要做）

初稿建议不做，理由是 Core 的创建入口是 Agent 工具（`tools/automation.py` 的 `action: "create"`），
CLI 里刻意没有 `task create`——定时任务的 prompt 更适合由 Agent 在对话中理解意图后生成。

**用户 2026-08-11 明确要求实现表单式新建**，已按以下收窄落地：

- 表单只收 `name` / `prompt` / `schedule` 三项，Core 支持的 `skills`/`delivery`/`budget` 不开放，
  在 protocol 层**拒绝而非忽略**——静默忽略会让调用方误以为生效；
- 调度类型只允许 `once`/`interval`/`cron`，`heartbeat` 是系统内部心跳不给创建入口（但既有的
  heartbeat 任务仍能在列表中正常显示）；
- `interval` 设 5 分钟下限，在界面、IPC、Core protocol 三处各校验一次，防止误配置高频空转烧 token。

对话中让 Agent 建任务这条路径**同时保留**，两者并不互斥。

### 4.4 安全门禁

- 所有写操作在 `taskBusy`（有回合在跑）时禁用，沿用现有 `taskBusy` 机制；
- `halt` 是破坏性操作（停掉所有自动化），必须二次确认并要求填写原因（Core 侧 `--reason` 本就必填）；
- `run`（立即运行）会真实触发一次 Agent 回合并消耗预算，需要明确的确认提示；
- `taskId` 必须是正整数，非法值在 protocol 层拒绝（复用 `_positive_cli_id` 的同款校验思路）。

### 4.5 TDD 起点

Python：protocol 的 exact-key 与非法 `taskId`；每个新请求类型的 dispatch 分支；busy 状态下的拒绝；
`halt` 缺 reason 时拒绝；不存在的 taskId 返回稳定错误码。
Desktop：统计数字的纯函数（从任务列表算 total/active/paused/failed）；capability 缺失时不渲染写操作
控件；破坏性操作的确认流程。

### 4.6 退出条件

1. 四张统计卡显示真实数字；
2. 暂停/恢复/立即运行/取消四个操作真实生效并在列表刷新后可见；
3. 运行历史可查看；
4. E-stop 可启用与解除，界面明确显示急停状态；
5. capability 缺失时优雅降级为只读；
6. Python + Desktop 全量门禁通过，真实 Electron + Bridge smoke 覆盖至少一次 pause→resume 往返。

---

## 5. D2b：模型与密钥可在界面配置

### 5.1 用户结果

- 设置页可以修改：**当前模型名**、**Provider base_url**、**API Key**；
- API Key 输入后写入 `secrets.env`（0600），**不写进 `config.toml`**，界面上永远不回显明文；
- 保存后需要重启 Bridge 才生效，界面明确告知并提供一键重启；
- 修改前后都有校验：base_url 必须是 https（本地回环可放行）、模型名非空、密钥格式合法。

### 5.2 范围决策：做「多 Provider 并存」（用户拍板）

初稿建议先做单 Provider 可编辑，把多 Provider 留待以后。**用户 2026-08-11 明确要求做多 Provider**，
对齐 ClawX 的 Models 页（OpenAI / OpenRouter / MiniMax 并存 + 标记 Default）。

这意味着 D2b 的范围显著大于初稿：`ProviderConfig` 要从单条改成有 id 的列表、`AgentConfig.model`
要能引用具体 Provider、既有 `config.toml` 需要平滑迁移、每个 Provider 各自一个密钥环境变量。
详细设计见独立文档（见 §5.7），不在本规划文档展开。

注意这与落地文档「不为未来多模型建设路由平台」的既定约束存在张力：本次做的是**配置层的多
Provider 并存与切换**，不是请求级的自动路由/fallback/负载均衡——后者仍不做。

### 5.3 安全设计（这是本 milestone 的核心难点）

密钥写入必须复用 `setup.py` 已有的安全机制，不能另起一套：

- `validate_secret_value()`：拒绝空值、前后空白、引号开头、NUL、任何换行符——防止一个值破坏
  整个 dotenv 文件或注入第二个变量；
- 0600 权限 + 原子写（临时文件 + `os.replace`）+ `fsync`；
- **只更新目标变量，保留文件里的其他变量**（现有 `write_fresh_setup` 是 fresh-only 的全量写，
  这里需要一个新的「就地更新单个变量」函数，是本 milestone 唯一的新安全代码，必须重点测试）；
- Bridge 请求里的密钥值**绝不进日志、不进 Turn 记录、不回传给 Renderer**；
- 界面上密钥输入框用 `type="password"`，已配置状态只显示「已配置」而非任何前缀/后缀。

### 5.4 Bridge 协议变更

| 类型 | payload | 说明 |
| --- | --- | --- |
| `config.model.set` | `{"model": str}` | 写 `config.toml` 的 `agent.model` |
| `config.provider.set` | `{"baseUrl": str, "timeoutSeconds": int}` | 写 `provider` 段，不含密钥 |
| `secret.set` | `{"name": str, "value": str}` | 就地更新 `secrets.env`；`name` 限定在白名单内 |
| `bridge.restart` | `{}` | 复用 `bridge-service.ts` 已有的重启链路 |

`secret.set` 的 `name` 必须在固定白名单里（模型密钥、各 Channel 凭据），**不接受任意变量名**——
否则等于给了 Renderer 写任意环境变量的能力。

### 5.5 TDD 起点

- 就地更新单变量：保留其他变量、值含特殊字符时拒绝、文件权限保持 0600、写入中断不留半文件；
- `secret.set` 的名称白名单：白名单外一律拒绝；
- 密钥不出现在任何日志/响应/异常信息里（用 sentinel 值扫描输出，沿用 `test_setup.py` 已有的手法）；
- base_url 校验：非 https 且非回环时拒绝；
- Desktop：密钥框不回显、保存后提示重启、重启失败时的回滚提示。

### 5.6 退出条件

1. 能在界面改模型名并重启后真实生效；
2. 能填入新 API Key，`secrets.env` 里其他变量不受影响、权限仍是 0600；
3. 密钥明文不出现在界面、日志、Turn 记录、Bridge 响应中（有测试证明）；
4. 非法输入被拒绝且提示明确；
5. 全量门禁 + 真实 smoke 通过。

---

## 6. 与原 D2 的关系

原 D2（附件/Composer 控件）不取消，顺延为 **D2c**。其中「模型选择器」「Agent 选择器」两项要按本文档
的结论调整：

- 模型选择器：D2b 做完后，Composer 里显示的模型名可以点击跳到设置页，而不是做一个只有一项的假下拉；
- Agent 选择器：Core 侧完全没有多 Agent 概念，D2c 里仍然只显示只读的 `Main Agent`，真正的选择器
  等 D4。

## 7. 用户已拍板（2026-08-11）

两条都选了**要**，与本文档最初的建议相反。据此调整：

| 项 | 本文档原建议 | 用户决定 | 影响 |
| --- | --- | --- | --- |
| 定时任务表单新建（§4.3） | 不做，让 Agent 在对话中创建 | **要做** | D2a 增加一个安全敏感的创建入口，需单独设计 schedule 表单与确认流程 |
| 多 Provider 并存（§5.2） | 先做单 Provider 可编辑 | **要做** | D2b 从"改几个字段"升级为"数据结构变更 + 配置迁移 + 首次引入配置写入能力" |

两个决定已分别落到独立设计文档：

- [D2a 定时任务从只读变可控（含表单新建）](2026-08-11-desktop-d2a-automation-control-design.md)
- [D2b 多 Provider 模型配置与密钥写入](2026-08-11-desktop-d2b-model-and-secret-config-design.md)

其中 D2b 的风险明显高于另外两个 milestone：它同时引入配置写入（Core 此前**从未**写回过
`config.toml`）、单数→复数的数据结构变更、密钥写入、以及不能破坏既有用户的配置迁移。因此实施顺序
定为 **D2a → D2b → D2c**，让 D2a 先把「Bridge 写操作」这套模式跑通，D2b 再在成熟模式上做更难的部分。
