# Phase 1 工程文档：AgentRunner

> 文档性质：`HISTORICAL SNAPSHOT`（Phase 1 交付状态）
>
> 当前替代：Phase 2.1A 已删除临时 `ToolHandler` Mapping，改为 Policy 控制的 `ToolExecutor`，并持久化
> 中间消息；当前实现见
> [Tool Runtime 与 system_info](../phase-2/tool-runtime-and-system-info.md)。

## 1. 模块目的

`src/miniclaw/agent/runner.py` 管理一次 Turn 内部的模型与 Tool Call 循环。它接收完整 `ModelRequest`，
重复调用 `ModelProvider.complete()`，按响应顺序执行工具，直到得到非空最终回答或达到 8 轮上限。

Runner 不知道消息来自 CLI、飞书、Telegram 还是 Discord，也不知道 SQLite。Channel 与持久化都由
TurnService 包裹在外层。

## 2. 核心状态机

```mermaid
flowchart TD
    START["ModelRequest"] --> CALL["Provider.complete"]
    CALL --> USAGE["累计 usage / request id"]
    USAGE --> TOOLS{"有 Tool Call?"}
    TOOLS -->|"否"| CONTENT{"content 非空?"}
    CONTENT -->|"是"| DONE["AgentRunResult"]
    CONTENT -->|"否"| EMPTY["EmptyModelResponseError"]
    TOOLS -->|"是"| LIMIT{"iteration == max?"}
    LIMIT -->|"是"| STOP["AgentLoopLimitError"]
    LIMIT -->|"否"| ASSISTANT["追加 Assistant + reasoning + calls"]
    ASSISTANT --> EXECUTE["按顺序执行每个 ToolCall"]
    EXECUTE --> RESULT["追加 Tool Message"]
    RESULT --> CALL
```

`max_iterations` 统计模型调用次数，不是 Tool 数量。默认值来自 `AgentConfig.max_tool_iterations=8`，名称为
历史兼容；语义以 Runner 的模型轮数为准。

## 3. 公共接口

```python
class AgentRunner:
    def __init__(
        self,
        provider: ModelProvider,
        tools: Mapping[str, ToolHandler] | None = None,
        *,
        max_iterations: int = 8,
    ) -> None: ...

    async def run(
        self,
        request: ModelRequest,
        on_text: StreamHandler | None = None,
    ) -> AgentRunResult: ...
```

工具执行边界：

```python
type ToolHandler = Callable[[dict[str, JsonValue]], Awaitable[str]]
```

Phase 1 默认 `tools={}`。测试提供一个 `echo` Handler 证明循环；真正文件、HTTP、Shell、Policy 和审批在
Phase 2 接入同一个映射边界。

## 4. AgentRunResult

| 字段 | 计算方式 | Storage 用途 |
| --- | --- | --- |
| `content` | 最终无 Tool Call 响应原文 | Assistant Message |
| `iterations` | 实际 Provider 调用数 | 运行诊断 |
| `input_tokens` | 所有响应非空 input usage 之和 | Turn.input_tokens |
| `output_tokens` | 所有响应非空 output usage 之和 | Turn.output_tokens |
| `provider_request_id` | 最后一个非空 request ID | runtime snapshot / 排障 |
| `finish_reason` | 最终响应结束原因 | runtime snapshot |

Provider 未返回用量时该轮按 0 聚合。原始契约仍用 `None` 表示缺失；Runner 的 Turn 汇总使用整数以匹配
SQLite 非空字段。

## 5. 不可变请求演进

每轮使用 `dataclasses.replace()` 创建新请求：

```python
current = replace(initial_request, messages=tuple(messages))
```

model、tools、temperature 和 max output budget 保持初始值；只追加消息。已经交给 Provider 或保存在 Fake
中的历史 Request 不会被后续轮原地修改，便于测试和任务回放。

## 6. Tool Call 继续消息

模型返回工具调用后，Runner 先追加完整 Assistant Message：

```python
ModelMessage(
    role="assistant",
    content=response.content,
    tool_calls=response.tool_calls,
    reasoning_content=response.reasoning_content,
)
```

再为每个调用追加：

```python
ModelMessage(
    role="tool",
    content=<handler result>,
    tool_call_id=call.call_id,
)
```

reasoning 必须与对应 Tool Call 同时回传 DeepSeek，否则思考模式的下一次请求会被 API 拒绝。

## 7. 工具执行顺序

一个响应最多包含多个 Tool Call。Phase 1/2 都按响应数组顺序逐个 `await`：

1. 避免并发文件写入顺序不确定；
2. 让审批和 Audit 顺序与模型输出一致；
3. 简化单用户任务回放。

未来只有在工具声明无副作用且性能测量证明需要时，才增加并行执行能力。

### 7.1 未注册工具

未注册工具不抛 Python 异常，而是返回确定性 JSON：

```json
{"ok":false,"error":"tool_not_found","tool":"missing"}
```

模型可以据此改用其他方案或向用户解释。Phase 2 的参数校验、Policy deny 与工具失败也会采用结构化 Tool
Result，但不会复用这一条错误码冒充不同原因。

## 8. 八轮上限

默认最多调用 Provider 8 次：

- 第 1–7 次返回 Tool Call：执行并继续；
- 第 8 次返回最终文本：成功；
- 第 8 次仍返回 Tool Call：立即抛 `AgentLoopLimitError`，不执行这批 Tool。

最后一批不执行是安全要求：已经没有下一次模型调用可消费结果，执行副作用没有用户可见收益。

错误消息包含配置上限，不含 Prompt、Tool 参数或模型响应。

## 9. 空响应

响应没有 Tool Call 且 `content.strip()` 为空时抛：

```text
EmptyModelResponseError: model returned an empty final response
```

Runner 不把空白保存为正常 Assistant Message。TurnService 会标记 `empty_response`，用户可以重试或检查
Provider request ID。

工具响应的 content 可以为空，因为 Tool Call 本身是有效结构；是否展示中间文本由 Channel 决定。

## 10. 流式回调

`run(..., on_text=...)` 把回调交给每次 Provider 调用。Provider 只发送 `content`，不发送 reasoning 和 Tool
arguments。

Phase 1 CLI 使用最终聚合结果，不传回调。后续 Channel 使用时需要在 Delivery 层区分中间 Tool Turn 与
最终回答；Runner 本身不加入平台展示规则。

## 11. 取消与异常

Runner 不捕获：

- `asyncio.CancelledError`；
- `ProviderError` 具体子类；
- ToolHandler 的编程错误。

原因：

- TurnService 必须把取消保存为 `cancelled`，不能误记为失败；
- CLI 需要用认证错误映射 exit 3，其他 Provider 错误映射 exit 4；
- Phase 2 会定义正式 ToolError 与 Policy 结果，当前不应把未知代码缺陷伪装成模型可恢复错误。

Runner 自己只产生 `EmptyModelResponseError` 和 `AgentLoopLimitError`，都继承 `AgentError`。

## 12. FakeProvider

`tests/fakes/fake_provider.py` 保存预设的完整响应或异常序列：

- 每次调用先记录真实 `ModelRequest`；
- 按索引返回响应；
- 预设异常按原类型抛出；
- 调用超过配置数量立即 AssertionError；
- 有流回调时用响应 content 模拟一次可见 delta。

Fake 只替代外部模型，不替代 ContextBuilder、Runner、TurnService 或 SQLite。测试断言真实模块的返回值和
状态，并检查记录的第二轮 Request 是否包含正确 reasoning/Tool Message。

## 13. 测试矩阵

`tests/test_agent_runner.py` 覆盖：

- 单轮最终文本、轮数、用量和 request ID；
- Tool → Handler → 下一次模型调用 → 最终回答；
- Assistant reasoning 与 Tool Call 原样续接；
- 多轮 Token 累加；
- 未注册工具的结构化结果；
- 空白最终响应；
- 第八轮仍请求工具时停止且最后 Tool 不执行；
- CancelledError 原样传播。

这些测试均使用真实 Runner 和手写模型结果，不访问网络。

## 14. 本地调试

聚焦测试：

```bash
uv run python -m unittest tests.test_agent_runner -v
```

定位循环问题时检查：

1. Fake/Provider 每轮请求数；
2. 第二轮 messages 最后两项是否为 Assistant Tool Calls 与 Tool Result；
3. Assistant 是否包含 `reasoning_content`；
4. Tool Message 的 `tool_call_id` 是否匹配；
5. max iterations 是否来自当前配置。

不要在调试输出中打印完整 Messages；它可能包含用户对话、reasoning 和工具结果。正式 Audit 只保存必要
摘要和哈希。

## 15. Phase 2 接入点

Phase 2 用一个受 PolicyEngine 控制的 Handler 替换当前测试 Handler：

```text
ToolCall
→ Registry 参数 Schema 校验
→ PolicyEngine.authorize
→ allow / deny / waiting approval
→ ToolResult JSON string
```

Provider、消息续接、轮数和 Token 聚合不改变。Waiting Approval 将返回结构化 Tool Result 并结束当前
Turn，由新的 approval Turn 续执行。

## 16. 已知限制与升级条件

- Phase 1 不持久化每个中间 Tool Call；Phase 2 增加 `tool_runs` Repository。
- ToolHandler 暂时返回字符串；Phase 2 升级为带 `ok/content/metadata` 的 ToolResult。
- 一个 Turn 内工具顺序执行，不并行。
- Token 缺失按 0 汇总，不估算。
- Runner 不做上下文压缩；Context Phase 负责。
- 只有第二个真实 Runner 策略出现时才提取策略接口，不为单实现创建 Factory。
