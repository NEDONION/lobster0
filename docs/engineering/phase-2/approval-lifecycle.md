# Phase 2.2B 工程文档：参数绑定 Approval SQLite 状态机

> 状态：canonical 参数哈希、waiting ToolRun、pending/approved/expired/consumed 和并发单次消费已实现
>
> 当前门禁：201/201 tests、10/10 offline Agent cases、Ruff PASS
>
> 当前非目标：`approvals` CLI、Turn `waiting_approval`、批准后的 child Turn 和模型续答在 P2.2C 接入

## 1. 大白话解释

Approval 不是一句“我同意 Agent 写文件”。它更像一张只对一次具体操作有效的电子签字单：

```text
工具名：write_file
目标：note.txt
内容和 overwrite：已经参与 hash，但不会显示在 Audit
有效期：10 分钟
次数：只能消费一次
```

只要工具名或任一参数变化，原签字就不能使用。状态全部在 SQLite，不依赖某个 Python 进程一直活着。

## 2. 当前链路

```mermaid
flowchart LR
    MODEL["Model Tool Call"] --> EXECUTOR["ToolExecutor"]
    EXECUTOR --> POLICY["PolicyEngine"]
    POLICY -->|"allow"| RUN["running ToolRun"]
    POLICY -->|"deny"| AUDIT["tool.denied audit"]
    POLICY -->|"require_approval"| CREATE["ApprovalRepository.create_waiting"]
    CREATE --> TR["ToolRun: waiting_approval"]
    CREATE --> AP["Approval: pending"]
    CREATE --> EA["approval.created audit"]
    EXECUTOR --> OUT["ToolExecution(model_text, approval_id)"]
```

`ToolExecutor.execute()` 不再返回一个无法携带业务状态的裸字符串，而是：

```python
@dataclass(frozen=True, slots=True)
class ToolExecution:
    model_text: str
    approval_id: int | None = None
```

普通 allow/deny 的 `approval_id` 是 `None`。只有真实写入 SQLite 的 waiting Approval 才返回 ID。

## 3. 为什么使用 SQLite，而不是内存等待

内存 ApprovalManager 有三个问题：

1. CLI 进程退出后审批丢失；
2. 未来飞书消息可能由另一个进程生命周期处理；
3. 两个批准请求可能同时执行同一副作用。

当前实现直接复用 Schema v1 的 `tool_runs`、`approvals`、`audit_events`，没有新增 migration、轮询线程或
busy-wait。重启后创建新的 `ApprovalRepository` 仍能查询同一条 pending 记录。

## 4. Canonical JSON 与 hash

完整规则：

- 对象键字典序；
- UTF-8，不转义中文；
- 紧凑分隔符；
- 只允许标准 JSON，拒绝 NaN/Infinity；
- Policy 先把文件路径解析成规范绝对路径；
- SHA-256 输入包含 Tool 名。

```text
canonical_json = {"content":"hello","overwrite":false,"path":"/safe/workspace/note.txt"}
hash_input = "write_file\n" + canonical_json
arguments_hash = sha256(hash_input)
```

因此：

- 参数键顺序不同，hash 相同；
- 内容、路径、overwrite 任一个变化，hash 不同；
- 同一参数从 `write_file` 换成 `edit_file`，hash 不同；
- 数据库中的 `arguments_json` 被修改但 hash 没同步，消费返回 `hash_mismatch`。

## 5. Policy 参数规范化

```mermaid
sequenceDiagram
    participant Tool as Tool.validate
    participant Policy as PolicyEngine
    participant Guard as WorkspaceGuard
    participant Exec as ToolExecutor
    participant DB as ApprovalRepository

    Tool->>Policy: raw validated args
    Policy->>Guard: resolve_write(relative path)
    Guard-->>Policy: canonical absolute path
    Policy-->>Exec: PolicyDecision + normalized_arguments
    Exec->>DB: create_waiting(normalized arguments)
```

Assistant 的原始 Tool Call 仍保留模型参数；本机加密边界内的 ToolRun 保存规范参数，供续执行和签名校验。
Audit 只保存 hash 前 12 位，不保存路径、content 或完整参数 JSON。

## 6. 创建事务

`ApprovalRepository.create_waiting()` 在同一个 SQLite 事务执行：

1. 插入 `tool_runs(status='waiting_approval', policy_action='require_approval')`；
2. 插入 `approvals(status='pending', expires_at=now+ttl)`；
3. 插入 `approval.created` 审计；
4. 返回不可变 `StoredApproval`。

任何一条 SQL 失败，三条记录一起回滚。Tool 尚未执行，因此不会出现“文件写了但审批没保存”。

## 7. 状态机

```mermaid
stateDiagram-v2
    [*] --> pending: create_waiting
    pending --> approved: approve(owner, id)
    pending --> expired: approve after expires_at
    approved --> consumed: consume + hash match
    approved --> expired: consume after expires_at
    consumed --> [*]
    expired --> [*]
```

P2.2B 已验证的迁移：

| Approval 迁移 | 绑定 ToolRun 迁移 | 条件 |
| --- | --- | --- |
| 新建为 `pending` | 新建为 `waiting_approval` | Policy 返回 require approval |
| `pending -> approved` | 保持 waiting | Owner 正确且未过期 |
| `pending/approved -> expired` | `waiting_approval -> denied` | 决策或消费时发现 TTL 到期 |
| `approved -> consumed` | `waiting_approval -> running` | Owner、TTL、两个 hash 与重算 hash 全部一致 |

`denied` 会在 P2.2C 与 CLI/模型续答一起加入，避免先实现一个没有消费方的半条拒绝链。

## 8. 原子单次消费

消费使用 SQLite `BEGIN IMMEDIATE`：

```mermaid
sequenceDiagram
    participant A as Consumer A
    participant DB as SQLite
    participant B as Consumer B

    A->>DB: BEGIN IMMEDIATE
    B->>DB: BEGIN IMMEDIATE (wait)
    A->>DB: verify owner/TTL/hash
    A->>DB: approved→consumed; waiting→running
    A->>DB: COMMIT
    DB-->>B: lock released
    B->>DB: sees consumed
    DB-->>B: already_decided
```

测试用两个真实线程和两个独立 SQLite connection 同时消费。结果必须恰好一个 `consumed`、一个
`already_decided`，最终只存在一条 `approval.consumed` Audit。

## 9. 持久化对象

### `StoredApproval`

包含 ID、Owner、原 Turn、ToolRun、Tool 名、参数 hash、脱敏摘要、状态和三个时间字段。它不包含写入内容。

### `StoredToolRun`

只有成功消费后返回，包含绑定 ToolRun ID、原 Tool Call ID、Tool 名、完整规范参数、hash 和 `running` 状态。
P2.2C 会把它交给 `ToolExecutor.execute_approved()`，不会重新经过一个可改变参数的模型请求。

## 10. 稳定错误码

| code | 触发条件 | 是否消费/执行 |
| --- | --- | --- |
| `not_found` | Approval ID 不存在 | 否 |
| `not_owner` | ID 存在但不属于当前 Owner | 否 |
| `expired` | TTL 到期 | 否；ToolRun 变 denied |
| `already_decided` | 已批准、已消费或其他非目标状态 | 否 |
| `hash_mismatch` | JSON 损坏、参数或 hash 被修改 | 否 |

底层数据库不可用不会伪装成上述业务错误，而是继续向上抛出，让 CLI 使用本地 I/O 失败退出码。

## 11. 审计与秘密边界

当前新增事件：

- `approval.created`
- `approval.approved`
- `approval.expired`
- `approval.consumed`

metadata 只包含：Approval ID、ToolRun ID、Tool 名、12 位 hash 前缀。测试使用
`private-content` 哨兵证明内容不会进入 `summary` 或 `metadata_json`。

完整参数只存在 owner-only 状态目录中的 SQLite `tool_runs.arguments_json`，这是批准后恢复执行所必需；
不会进入普通日志或进度页面。

## 12. 测试证据

聚焦门禁：

```bash
uv run python -m unittest tests.test_approvals tests.test_tool_executor tests.test_agent_runner -v
uv run ruff check src/miniclaw/policy/approvals.py src/miniclaw/policy/engine.py \
  src/miniclaw/storage/tooling.py src/miniclaw/tools/executor.py src/miniclaw/agent/runner.py \
  tests/test_approvals.py tests/test_tool_executor.py
```

结果：28/28 通过。

全仓门禁：201/201 tests、10/10 offline Agent cases、Ruff PASS、diff check PASS。

## 13. 当前边界与 P2.2C

- `ApprovalRepository.list/get` 当前只查询，不在读取时惰性过期；P2.5 硬化会补齐。
- `ToolExecutor` 只有配置了 Approval Repository 才创建真实记录；生产 CLI 会在 P2.2C 一次性完成组装。
- Runner 目前读取 `ToolExecution.model_text`，还不会在 `approval_id` 出现时停止模型循环。
- 当前没有 `deny`、`--always` 或 PolicyRule；它们与实际 CLI 决策入口一起实现。
- 当前不会执行 consumed ToolRun；`execute_approved` 和 child Turn 是下一任务的唯一功能重点。
