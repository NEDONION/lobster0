# 交互式 setup 补齐 Owner ID 获取指引

> 日期：2026-08-11
> 文档类型：可用性缺口修复实现记录
> 状态：`IMPLEMENTED`（2026-08-11）

## 1. 触发来源

用户在真实跑 `start-desktop.command` 首次配置时卡住，原话：

> 还有就是【Enable Feishu? [y/N]: y / Feishu Owner open_id: 】这一块你不给指引 谁知道去哪里能找到 open_id

`open_id`、Telegram/Discord 的数字 user ID 都不是用户日常能看到的值——飞书客户端界面上根本不显示
`ou_` 开头的 open_id，Telegram/Discord 的数字 ID 默认也不可见（Discord 还要先打开开发者模式）。
向导直接问却不说来源，等于把用户拦在第一步。

## 2. 根因

[src/lobster0/setup.py](../../src/lobster0/setup.py) 的 `run_interactive_setup` 三处提问都是裸提示
字符串，没有任何上下文：

```python
_ask_text(tty, "Feishu Owner open_id: ")
_ask_owner_user_id(tty, "Telegram Owner user ID: ")
_ask_owner_user_id(tty, "Discord Owner user ID: ")
```

这不是设计遗漏（Owner 机制本身是对的，见 Phase 4 飞书 Channel 设计），纯粹是交互文案缺口。

## 3. 方案

在每个 Owner 提问前先输出一段获取指引。实现上给 `_ask_text` / `_ask_owner_user_id` 增加一个
keyword-only 的 `hint: str | None = None` 参数，在写 prompt 前先写 hint：

- **不改函数签名的位置参数**，`hint` 是 keyword-only 且有默认值，所有既有调用点不受影响；
- **不改任何校验逻辑**：`_FEISHU_OWNER` 正则、`_ask_owner_user_id` 的 `int()` 解析与 `SetupError`
  语义全部原样保留；
- 三段指引提取为模块级常量 `_FEISHU_OWNER_HINT` / `_TELEGRAM_OWNER_HINT` / `_DISCORD_OWNER_HINT`，
  与既有 `_MODEL_SECRET` 等常量并列，便于后续维护和测试引用。

指引内容（均为公开可查信息，不含任何 Secret）：

| 平台 | 指引要点 |
| --- | --- |
| 飞书 | 说明 Owner 含义 + 格式 `ou_xxx`；开放平台 open.feishu.cn 开发者后台路径；已装 lark-cli 时可直接 `lark-cli auth status` 读 `openId` |
| Telegram | 私聊 `@userinfobot` 直接回复数字 ID |
| Discord | 设置 →「高级设置」开启开发者模式 → 右键头像「复制用户 ID」 |

每段都先解释"Owner 是唯一能私聊指挥这个 Bot 的人"，因为用户不理解这个字段的作用时，就算知道怎么
取值也不知道该填谁的。

## 4. 验证

- 新增 `test_interactive_setup_explains_where_to_find_each_owner_id`：三个 Channel 全启用走完整
  交互流程，断言 TTY 输出里出现 `ou_xxx`、`lark-cli auth status`、`@userinfobot`、`开发者模式`
  四个关键指引片段，防止以后被无意删除；
- `tests/test_setup.py` 15/15 通过（原 14 + 新增 1）；
- `tests.test_setup` + `tests.test_cli` + `tests.test_install_orchestrator` 合计 109/109 通过——
  这三个模块覆盖了 `run_interactive_setup` 的全部既有调用点（含真实 PTY 双工 setup 用例）；
- `ruff check` 通过，`git diff --check` 通过；
- 人工核对了渲染效果，确认缩进和换行在真实终端里对齐。

既有测试之所以不受影响：它们采用"喂输入序列 + 断言最终配置/Secret 落盘结果"的模式，不断言提示
文案本身，新增输出不改变任何被断言的行为。

## 5. 影响范围

仅 `src/lobster0/setup.py`（常量 + 两个 helper 的可选参数 + 三处调用）与 `tests/test_setup.py`
（新增一个测试）。不涉及配置格式、Secret 处理、Owner 校验规则、Channel 启用逻辑或任何持久化行为。

## 6. 遗留

- 本次只补了交互式 setup 的指引。`docs/getting-started/20260807_本地运行指南.md` 里手写
  `owner_open_id = "ou_replace_with_owner"` 的示例同样没有说明来源，后续可以把同一段指引同步过去；
- 非交互安装路径（installer 直接写配置）不经过这些提问，不受影响也无需指引。
