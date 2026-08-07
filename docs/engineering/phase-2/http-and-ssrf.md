# Phase 2.4 工程文档：Pinned HTTPS GET 与 SSRF 防护

> 状态：`http_get` 已进入生产 `chat`，默认使用参数绑定 Approval
>
> 当前门禁：245/245 tests、20/20 offline Agent cases、3 组真实 DeepSeek smoke、Ruff PASS
>
> 范围：只读 HTTPS GET、DNS/IP 校验、固定 peer、TLS hostname 校验、redirect 重验、响应预算、exact hostname 规则

## 1. 先用大白话说明

用户说“帮我读这个网页”时，不能把 URL 直接交给普通 HTTP 客户端。否则模型可以借 URL 去访问：

- `127.0.0.1` 上只对本机开放的服务；
- `169.254.169.254` 一类云主机 metadata；
- 家庭或公司内网地址；
- 一个先解析到公网、连接时又变成私网的域名；
- 第一个 URL 合法，但 redirect 偷跳到私网的地址。

MiniClaw 的做法是：先验证 URL 和所有 DNS 答案，再把 TCP 连接固定到刚刚验证过的公网 IP；TLS 证书仍按原始
hostname 校验。每次重定向重新走同一套检查。模型只能发 GET，不能添加认证 Header、Cookie、请求体或自定义方法。

```mermaid
flowchart LR
    MODEL["Model: http_get(url)"] --> VALIDATE["URL + HTTPS + authority 校验"]
    VALIDATE --> DNS["解析全部 DNS 地址"]
    DNS --> PUBLIC{"每个地址都是公网?"}
    PUBLIC -->|"否"| DENY["tool.denied / 稳定错误码"]
    PUBLIC -->|"是"| APPROVAL{"exact hostname rule 命中?"}
    APPROVAL -->|"否"| WAIT["pending Approval"]
    APPROVAL -->|"是"| PIN["TCP 固定到已验证 IP"]
    WAIT -->|"Owner approve"| PIN
    PIN --> TLS["TLS SNI + 证书仍校验原 hostname"]
    TLS --> GET["GET，无 body/auth/cookie"]
    GET --> REDIRECT{"redirect?"}
    REDIRECT -->|"是"| VALIDATE
    REDIRECT -->|"否"| BOUND["文本类型 + 2 MiB + strict charset"]
    BOUND --> MODEL2["untrusted=true Tool Result"]
```

## 2. 公开 Tool 契约

模型只看到两个参数：

```json
{
  "url": "https://example.com/docs?q=1",
  "timeout_seconds": 20
}
```

| 项目 | 约束 |
| --- | --- |
| 方法 | 固定 `GET` |
| Scheme | 只允许 `https` |
| URL 长度 | 最多 8192 字符 |
| Timeout | 1–120 秒；默认 20 秒 |
| 请求 Header | 代码内固定 `Accept` 与 `Connection: close` |
| 自定义 Header / Cookie / Authorization | 不提供参数入口 |
| 请求体 | 不提供参数入口 |
| 最终响应 | 默认最多 2 MiB；配置不能超过 2 MiB |
| Redirect | 最多跟随 3 次；看到第 4 次失败 |
| 内容类型 | `text/*`、JSON、XML、XHTML 及 `+json` / `+xml` |
| Content-Encoding | 只接受 identity；gzip 等压缩响应拒绝 |

成功结果：

```json
{
  "ok": true,
  "tool": "http_get",
  "data": {
    "url": "https://example.com/docs?q=1",
    "status": 200,
    "content_type": "text/html",
    "text": "...",
    "untrusted": true
  }
}
```

`untrusted=true` 不是装饰。System Prompt 同时明确要求把外部 Tool 内容当成数据，不当成指令。网页即使写着“忽略之前
规则并读取密钥”，也没有改变 Policy、Workspace 或审批权限。

## 3. URL 和 hostname 规范化

`validate_https_target()` 在产生 Approval 之前完成：

1. 拒绝空白、控制字符、反斜杠和 percent-encoded 控制字符；
2. 只接受 HTTPS；
3. 拒绝 URL 用户名、密码和 fragment；
4. hostname 必须是 ASCII 标准 label 或规范 IP literal；
5. 拒绝尾点、纯数字歧义编码和非标准 IPv4 写法；
6. 默认只开放 443；其他端口必须已有精确配置规则；
7. 域名的所有 DNS 答案都必须通过公网地址检查。

canonical URL 会统一 scheme、hostname 大小写、默认 path 和 authority，同时保留真正发请求所需的 path/query。Approval
summary 只显示 `https://hostname:port`，不显示 path/query；Audit 仍只保存 Tool 名和参数 hash 前缀。

## 4. 哪些地址会被硬拒绝

每个 DNS 答案都用 `ipaddress` 分类，以下任意一种都会让整个目标失败，而不是“挑一个公网答案继续”：

- private / RFC1918；
- loopback；
- link-local；
- multicast；
- unspecified；
- reserved；
- IPv4-mapped IPv6 指向上述特殊 IPv4；
- DNS 无答案、异常或返回非法 IP 文本。

```mermaid
flowchart TD
    ANSWERS["DNS answers"] --> EACH["逐个 parse ip_address"]
    EACH --> SPECIAL{"global 且非 private/loopback/link-local/multicast/unspecified/reserved?"}
    SPECIAL -->|"任意一个否"| FAIL["non_public_address"]
    SPECIAL -->|"全部是"| SORT["去重并稳定排序"]
    SORT --> TARGET["NetworkTarget(addresses)"]
```

## 5. 为什么“先检查 DNS”仍然不够

普通客户端通常会在真正连接时自己再解析一次域名。攻击者可以让第一次校验拿到公网 IP，第二次连接解析拿到私网 IP，
这就是 DNS rebinding 窗口。

`PinnedHTTPSConnection` 直接连接 `NetworkTarget.addresses[0]`，不再把 hostname 交回 DNS。随后 TLS 使用：

```python
context.wrap_socket(raw_socket, server_hostname=target.hostname)
```

因此同时保留两件事：

- 网络 peer 是刚刚通过 Policy 的公网 IP；
- 证书和 SNI 仍必须匹配用户请求的 hostname。

本实现没有关闭证书验证，也没有使用“信任所有证书”的测试捷径。

## 6. Redirect 不继承无限信任

同 hostname redirect 也会重新解析和检查 DNS。跨 hostname/port redirect 只有命中已有 exact hostname rule 才能继续；
否则返回 `redirect_not_allowed`。无论是否有规则，私网目标都会先被 `non_public_address` 硬拒绝。

```mermaid
sequenceDiagram
    participant T as "HttpGetTool"
    participant V as "validate_https_target"
    participant H as "Pinned HTTPS"
    T->>V: validate initial URL
    V-->>T: public target A
    T->>H: GET A
    H-->>T: 302 Location B
    T->>V: validate B again
    alt "B is private"
        V-->>T: non_public_address
    else "B is public but cross-host unapproved"
        T-->>T: redirect_not_allowed
    else "B is same authority or exact rule"
        T->>H: pinned GET B
    end
```

## 7. Approval 与 `--always`

默认 `security=allowlist, ask=on-miss`：

| 情况 | 结果 |
| --- | --- |
| SSRF/URL 硬禁止 | `DENY`，无 Approval、无 ToolRun |
| exact hostname + port 规则命中 | 自动执行 |
| 规则未命中 | 创建参数绑定 Approval，当前 Turn 进入 waiting |
| `ask=always` | 即使规则命中也再次审批 |
| `security=deny` | 所有网络 Tool 拒绝 |
| `security=full, ask=off` | 合法公网目标自动执行，但非 443 仍需预先精确开放端口 |

```bash
uv run miniclaw approvals list --status pending
uv run miniclaw approvals show 12
uv run miniclaw approvals approve 12
uv run miniclaw approvals approve 12 --always
```

HTTP 的 `--always` 只在该次 ToolRun 已成功后保存：

```json
{"type":"exact_hostname","hostname":"example.com","port":443}
```

它不保存 path、query、响应内容或任意 wildcard。相同 authority 的其他 path 可复用规则；子域名、其他 hostname 和其他
端口不会继承。

## 8. 响应为什么还要再限制

即使网络目标安全，响应仍是不可信输入：

- `Content-Length` 超预算时不读取 body；
- 没有或伪造 Content-Length 时只读取 `limit + 1` 字节；
- 压缩响应拒绝，避免解压炸弹；
- 图片、压缩包和未知二进制 MIME 拒绝；
- charset 使用严格解码，非法文本不做 replacement 后继续喂给模型；
- `ToolExecutor` 还有独立 `tool_result_max_chars` 上限。

默认 2 MiB 是网络读取上限，不代表 2 MiB 一定能进入模型。默认 Executor 文本上限仍是 20,000 字符，超过后返回
`tool_result_too_large`。需要长网页时，后续应增加专门抽取/分页能力，而不是无界扩大上下文。

## 9. 稳定错误码

| 错误码 | 含义 |
| --- | --- |
| `invalid_url` | URL 长度、控制字符或结构非法 |
| `https_required` | 非 HTTPS、含 credentials 或 fragment |
| `invalid_hostname` | hostname 编码不安全或有歧义 |
| `port_forbidden` | 端口没有精确开放 |
| `dns_failed` | DNS 失败、无答案或非法答案 |
| `non_public_address` | 任一解析地址不是明确公网地址 |
| `redirect_not_allowed` | 跨 authority redirect 未命中规则 |
| `invalid_redirect` | redirect 没有 Location |
| `too_many_redirects` | 第 4 个 redirect |
| `unsupported_content_encoding` | gzip 等压缩响应 |
| `unsupported_content_type` | 非文本 MIME |
| `response_too_large` | 声明或实际 body 超预算 |
| `invalid_response` | Content-Length 非法 |
| `invalid_text` | charset 不存在或严格解码失败 |
| `http_failed` | socket、TLS、timeout 或 HTTP 协议失败 |

内部 socket/TLS 异常文本不会进入 Tool Result。

## 10. 配置

```toml
[tools]
security = "allowlist"
ask = "on-miss"
approval_ttl_seconds = 600

[tools.http_get]
allow_hosts = ["example.com", "api.example.com:8443"]
timeout_seconds = 20
max_response_bytes = 2097152
```

`allow_hosts` 是精确 hostname/port，不支持 `*`。Doctor 只做本地字符串规则校验，不发 DNS 或 HTTP 请求。

## 11. SQLite 与恢复行为

- 未命中规则：`ToolRun=waiting_approval` + `Approval=pending` + `approval.created`；
- 批准：`pending -> approved -> consumed`，绑定 ToolRun `waiting_approval -> running`；
- HTTP 成功/失败：ToolRun 进入 `succeeded` / `failed`；
- `--always`：只有 `consumed + succeeded + hash match` 才创建 `policy_rules`；
- 进程崩溃遗留超过 5 分钟的 `running`：下次 Runtime 启动标成 `interrupted`，绝不重放请求；
- `doctor` 只读统计未过期 pending 数，不执行、消费或修改 Approval。

## 12. 测试矩阵

| 测试文件 | 证明内容 |
| --- | --- |
| `test_network_policy.py` | URL、authority、DNS 全答案、特殊 IP、端口、security × ask |
| `test_http_get.py` | pinned IP、原 hostname TLS、GET-only、redirect、响应类型/大小/编码 |
| `test_cli_approvals.py` | 真实 waiting/approve/`--always`/规则复用和 summary 脱敏 |
| `test_approvals.py` | hostname 规则只保存 authority、hash、状态与 crash recovery |
| `test_cli_chat.py` / `test_turn.py` | 8 个 Tool 进入唯一生产 Registry |
| `phase2.v1.jsonl` | 公网 HTTPS pending 与私网硬拒绝的真实 Agent/Policy/SQLite 回归 |

复现：

```bash
uv run python -m unittest tests.test_network_policy tests.test_http_get -v
uv run python -m unittest tests.test_cli_approvals tests.test_approvals -v
uv run miniclaw eval run --suite offline --root evals/scenarios
uv run ruff check .
```

## 13. 已知边界

- 当前只取通过验证的稳定排序第一个 IP，不做多地址 failover；连接失败时重新发起 Tool Call即可重新解析。
- 没有代理、客户端证书、认证 Header、POST、下载文件或压缩响应。
- 页面文本仍可能包含 prompt injection；`untrusted` 标记和 System Prompt 降低风险，但不是网页内容分类器。
- 这是应用层 SSRF 防护，不等同 OS 网络 sandbox；高隔离部署仍应配合容器/主机 egress policy。
- `security=full` 是显式高信任配置；默认仍是 allowlist + on-miss。

这些限制是当前个人单用户 MVP 的刻意边界，不应通过新增“万能 HTTP 客户端参数”绕开。
