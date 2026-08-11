"""Python Core 与 pi-tui 之间 NDJSON protocol v1 的契约测试。"""

import json
import unittest

from lobster0.bridge.protocol import (
    MAX_FRAME_BYTES,
    BridgeFrame,
    ProtocolError,
    decode_request,
    encode_frame,
)


class BridgeProtocolTest(unittest.TestCase):
    """验证跨语言帧只接受受限、可诊断且可安全编码的数据。"""

    def test_valid_turn_start_decodes_typed_request(self) -> None:
        """合法中文 Turn 必须保留请求 ID、Session 和原始多行文本。"""
        request = decode_request(
            b'{"v":1,"id":"req-1","type":"turn.start",'
            b'"payload":{"session_key":"default","text":"\xe4\xbd\xa0\xe5\xa5\xbd\\nLobster0"}}\n'
        )

        self.assertEqual(request.version, 1)
        self.assertEqual(request.request_id, "req-1")
        self.assertEqual(request.type, "turn.start")
        self.assertEqual(request.payload, {"session_key": "default", "text": "你好\nLobster0"})

    def test_invalid_frames_return_stable_codes_without_parser_details(self) -> None:
        """版本、UTF-8、结构和长度错误必须使用稳定安全码。"""
        cases = (
            (b"\xff\n", "invalid_encoding"),
            (b"{bad json}\n", "invalid_json"),
            (b"[]\n", "invalid_envelope"),
            (b'{"v":2,"id":"r","type":"turn.cancel","payload":{}}\n', "unsupported_version"),
            (b'{"v":1,"type":"turn.cancel","payload":{}}\n', "invalid_request_id"),
            (b'{"v":1,"id":"r","type":"turn.cancel","payload":[]}\n', "invalid_payload"),
            (b'{"v":1,"id":"r","type":"tool.execute","payload":{}}\n', "unknown_request"),
            (b" " * (MAX_FRAME_BYTES + 1), "frame_too_large"),
        )

        for raw, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(ProtocolError) as captured:
                    decode_request(raw)
                self.assertEqual(captured.exception.code, expected_code)
                self.assertNotIn("Traceback", str(captured.exception))
                self.assertNotIn("line 1 column", str(captured.exception))

    def test_request_specific_fields_fail_closed(self) -> None:
        """请求正文和审批决定必须满足长度、类型与枚举边界。"""
        cases = (
            (
                {"v": 1, "id": "r", "type": "turn.start", "payload": {"session_key": "default"}},
                "invalid_turn",
            ),
            (
                {
                    "v": 1,
                    "id": "r",
                    "type": "approval.resolve",
                    "payload": {"approval_id": 7, "decision": "everything"},
                },
                "invalid_approval",
            ),
            (
                {"v": 1, "id": "r", "type": "session.new", "payload": {"session_key": ""}},
                "invalid_session",
            ),
            (
                {
                    "v": 1,
                    "id": "r",
                    "type": "permissions.set",
                    "payload": {"mode": "unlimited"},
                },
                "invalid_permission_mode",
            ),
        )

        for frame, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                raw = json.dumps(frame).encode("utf-8") + b"\n"
                with self.assertRaises(ProtocolError) as captured:
                    decode_request(raw)
                self.assertEqual(captured.exception.code, expected_code)

    def test_permission_mode_request_accepts_only_the_four_exact_modes(self) -> None:
        """跨语言权限切换必须是精确枚举，不能接收额外字段或宽松别名。"""
        for mode in ("safe", "smart", "autopilot", "yolo"):
            with self.subTest(mode=mode):
                request = decode_request(
                    json.dumps(
                        {
                            "v": 1,
                            "id": f"mode-{mode}",
                            "type": "permissions.set",
                            "payload": {"mode": mode},
                        }
                    ).encode("utf-8")
                    + b"\n"
                )
                self.assertEqual(request.payload, {"mode": mode})

        for payload in ({}, {"mode": "AUTOPILOT"}, {"mode": "safe", "force": True}):
            with self.subTest(payload=payload):
                raw = json.dumps(
                    {"v": 1, "id": "mode-bad", "type": "permissions.set", "payload": payload}
                ).encode("utf-8") + b"\n"
                with self.assertRaises(ProtocolError) as captured:
                    decode_request(raw)
                self.assertEqual(captured.exception.code, "invalid_permission_mode")

    def test_memory_command_accepts_only_action_specific_fields(self) -> None:
        """Memory UI 请求不能携带 Owner/scope/status，并按 action 收窄参数。"""
        accepted = (
            {"action": "status"},
            {"action": "flush"},
            {"action": "rebuild"},
            {"action": "list", "limit": 10},
            {"action": "search", "query": "中文回复", "limit": 5},
            {"action": "why", "unit_id": "mem-123"},
            {"action": "review", "limit": 20},
            {"action": "forget", "unit_id": "mem-123"},
            {"action": "approve", "review_id": 7, "preview_hash": "a" * 64},
            {"action": "reject", "review_id": 7, "preview_hash": "a" * 64},
        )
        for index, payload in enumerate(accepted):
            request = decode_request(
                json.dumps(
                    {"v": 1, "id": f"memory-{index}", "type": "memory.command", "payload": payload}
                ).encode("utf-8")
                + b"\n"
            )
            self.assertEqual(request.payload, payload)

        rejected = (
            {"action": "search", "query": "中文", "owner_id": 1},
            {"action": "search"},
            {"action": "why", "unit_id": ""},
            {"action": "approve", "review_id": 7, "preview_hash": "short"},
            {"action": "reject", "review_id": True, "preview_hash": "a" * 64},
            {"action": "forget", "unit_id": "mem-123", "approve": True},
            {"action": "rebuild", "force": True},
        )
        for payload in rejected:
            with self.assertRaises(ProtocolError) as captured:
                decode_request(
                    json.dumps(
                        {"v": 1, "id": "memory-bad", "type": "memory.command", "payload": payload}
                    ).encode("utf-8")
                    + b"\n"
                )
            self.assertEqual(captured.exception.code, "invalid_memory_command")

    def test_session_queries_accept_only_bounded_read_parameters(self) -> None:
        """Desktop 会话查询不能携带 Owner、SQL 范围或无界 limit。"""
        accepted = (
            ("session.list", {"limit": 20}),
            ("session.history", {"session_key": "task-1", "limit": 100}),
        )
        for request_type, payload in accepted:
            request = decode_request(
                json.dumps(
                    {"v": 1, "id": "session-ok", "type": request_type, "payload": payload}
                ).encode("utf-8")
                + b"\n"
            )
            self.assertEqual(request.payload, payload)

        rejected = (
            ("session.list", {"limit": True}),
            ("session.list", {"limit": 51}),
            ("session.list", {"limit": 20, "owner_id": 1}),
            ("session.history", {"session_key": "", "limit": 100}),
            ("session.history", {"session_key": "task-1", "limit": 201}),
        )
        for request_type, payload in rejected:
            with self.subTest(request_type=request_type, payload=payload):
                with self.assertRaises(ProtocolError) as captured:
                    decode_request(
                        json.dumps(
                            {
                                "v": 1,
                                "id": "session-bad",
                                "type": request_type,
                                "payload": payload,
                            }
                        ).encode("utf-8")
                        + b"\n"
                    )
                self.assertEqual(captured.exception.code, "invalid_session_query")

    def test_automation_list_accepts_only_a_bounded_read_limit(self) -> None:
        """Automation 只读查询不能接收 Owner、bool 或无界数量。"""
        request = decode_request(
            b'{"v":1,"id":"automation-ok","type":"automation.list","payload":{"limit":50}}\n'
        )
        self.assertEqual(request.payload, {"limit": 50})

        for payload in (
            {"limit": True},
            {"limit": 0},
            {"limit": 101},
            {"limit": 50, "owner_id": 1},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ProtocolError) as captured:
                    decode_request(
                        json.dumps(
                            {
                                "v": 1,
                                "id": "automation-bad",
                                "type": "automation.list",
                                "payload": payload,
                            }
                        ).encode("utf-8")
                        + b"\n"
                    )
                self.assertEqual(captured.exception.code, "invalid_automation_query")

    def _decode(self, request_type: str, payload: object) -> object:
        """用给定类型与 payload 解码一帧，供 D2a 写操作的边界测试复用。"""
        return decode_request(
            json.dumps(
                {"v": 1, "id": "d2a", "type": request_type, "payload": payload}
            ).encode("utf-8")
            + b"\n"
        )

    def test_automation_task_actions_require_a_positive_integer_task_id(self) -> None:
        """pause/resume/run/cancel 只接受正整数 task_id，且不接受额外字段。"""
        for request_type in (
            "automation.pause",
            "automation.resume",
            "automation.run",
            "automation.cancel",
        ):
            with self.subTest(request_type=request_type, case="valid"):
                request = self._decode(request_type, {"task_id": 7})
                self.assertEqual(request.payload, {"task_id": 7})

            for payload in (
                {"task_id": 0},
                {"task_id": -1},
                {"task_id": True},
                {"task_id": 1.0},
                {"task_id": "1"},
                {},
                {"task_id": 1, "reason": "x"},
            ):
                with self.subTest(request_type=request_type, payload=payload):
                    with self.assertRaises(ProtocolError) as captured:
                        self._decode(request_type, payload)
                    self.assertEqual(captured.exception.code, "invalid_automation_action")

    def test_automation_runs_bounds_the_history_limit(self) -> None:
        """运行历史查询必须同时给 task_id 与有界 limit。"""
        request = self._decode("automation.runs", {"task_id": 3, "limit": 20})
        self.assertEqual(request.payload, {"task_id": 3, "limit": 20})

        for payload in ({"task_id": 3}, {"task_id": 3, "limit": 0}, {"task_id": 3, "limit": 101}):
            with self.subTest(payload=payload):
                with self.assertRaises(ProtocolError) as captured:
                    self._decode("automation.runs", payload)
                self.assertEqual(captured.exception.code, "invalid_automation_action")

    def test_automation_halt_requires_a_non_blank_bounded_reason(self) -> None:
        """急停必须带可审计的原因；空白理由等同没写。"""
        request = self._decode("automation.halt", {"reason": "误配置刷屏"})
        self.assertEqual(request.payload, {"reason": "误配置刷屏"})
        self.assertEqual(self._decode("automation.unhalt", {}).payload, {})

        for payload in ({}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 501}):
            with self.subTest(payload=payload):
                with self.assertRaises(ProtocolError) as captured:
                    self._decode("automation.halt", payload)
                self.assertEqual(captured.exception.code, "invalid_automation_action")

    def test_automation_create_enforces_schedule_and_interval_floor(self) -> None:
        """创建任务的字段收窄到"什么时候、跑什么"，并挡住高频 interval。"""
        request = self._decode(
            "automation.create",
            {
                "name": "每日摘要",
                "prompt": "汇总昨天的飞书文档",
                "schedule": {
                    "kind": "cron",
                    "expression": "0 9 * * *",
                    "timezone": "Asia/Shanghai",
                },
            },
        )
        self.assertEqual(request.payload["name"], "每日摘要")

        # timezone 可省略，Core 侧默认 UTC。
        self.assertEqual(
            self._decode(
                "automation.create",
                {
                    "name": "n",
                    "prompt": "p",
                    "schedule": {"kind": "interval", "expression": "300"},
                },
            ).payload["schedule"]["expression"],
            "300",
        )

        _cron = {"kind": "cron", "expression": "* * * * *"}
        for payload in (
            # 名称与 prompt 的边界
            {"name": "", "prompt": "p", "schedule": _cron},
            {"name": "n", "prompt": "", "schedule": _cron},
            {"name": "x" * 65, "prompt": "p", "schedule": _cron},
            {"name": "n", "prompt": "x" * 4001, "schedule": _cron},
            # heartbeat 是系统内部用途，不允许从界面创建
            {"name": "n", "prompt": "p", "schedule": {"kind": "heartbeat", "expression": "60"}},
            # interval 下限 5 分钟，防止误配置高频空转烧 token
            {"name": "n", "prompt": "p", "schedule": {"kind": "interval", "expression": "299"}},
            {"name": "n", "prompt": "p", "schedule": {"kind": "interval", "expression": "abc"}},
            # schedule 自身的形状
            {"name": "n", "prompt": "p", "schedule": {"kind": "nope", "expression": "x"}},
            {"name": "n", "prompt": "p", "schedule": {"kind": "cron"}},
            {
                "name": "n",
                "prompt": "p",
                "schedule": {"kind": "cron", "expression": "x", "extra": 1},
            },
            {"name": "n", "prompt": "p"},
            # 不开放的字段一律拒绝，避免绕过界面收窄
            {
                "name": "n",
                "prompt": "p",
                "schedule": _cron,
                "budget": {"max_turns": 999},
            },
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ProtocolError) as captured:
                    self._decode("automation.create", payload)
                self.assertEqual(captured.exception.code, "invalid_automation_action")

    def test_providers_list_takes_no_arguments(self) -> None:
        """只读查询不接收任何字段，避免被塞进越权参数。"""
        self.assertEqual(self._decode("providers.list", {}).payload, {})
        with self.assertRaises(ProtocolError) as captured:
            self._decode("providers.list", {"owner_id": 1})
        self.assertEqual(captured.exception.code, "invalid_provider_action")

    def test_provider_upsert_bounds_every_field(self) -> None:
        """新增/更新只接受 id、base_url、timeout，且 id 必须是安全字符集。"""
        request = self._decode(
            "providers.upsert",
            {
                "id": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "timeout_seconds": 120,
            },
        )
        self.assertEqual(request.payload["id"], "openrouter")

        for payload in (
            {"id": "Bad Id", "base_url": "https://a.example/v1", "timeout_seconds": 120},
            {"id": "UPPER", "base_url": "https://a.example/v1", "timeout_seconds": 120},
            {"id": "", "base_url": "https://a.example/v1", "timeout_seconds": 120},
            {"id": "x" * 33, "base_url": "https://a.example/v1", "timeout_seconds": 120},
            {"id": "a", "base_url": "", "timeout_seconds": 120},
            {"id": "a", "base_url": "https://a.example/v1", "timeout_seconds": 0},
            {"id": "a", "base_url": "https://a.example/v1", "timeout_seconds": 3601},
            {"id": "a", "base_url": "https://a.example/v1"},
            # api_key_env 由 Core 从 id 推导，不接受调用方指定
            {
                "id": "a",
                "base_url": "https://a.example/v1",
                "timeout_seconds": 120,
                "api_key_env": "PATH",
            },
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ProtocolError) as captured:
                    self._decode("providers.upsert", payload)
                self.assertEqual(captured.exception.code, "invalid_provider_action")

    def test_provider_select_requires_id_and_model(self) -> None:
        """切换默认必须同时指定模型名，避免留下空模型。"""
        request = self._decode("providers.select", {"id": "a", "model": "gpt-5"})
        self.assertEqual(request.payload, {"id": "a", "model": "gpt-5"})

        for payload in (
            {"id": "a"},
            {"model": "m"},
            {"id": "a", "model": ""},
            {"id": "a", "model": "   "},
            {"id": "a", "model": "x" * 201},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ProtocolError) as captured:
                    self._decode("providers.select", payload)
                self.assertEqual(captured.exception.code, "invalid_provider_action")

    def test_provider_remove_takes_only_an_id(self) -> None:
        """删除只认 id。"""
        self.assertEqual(self._decode("providers.remove", {"id": "a"}).payload, {"id": "a"})
        with self.assertRaises(ProtocolError):
            self._decode("providers.remove", {"id": "a", "force": True})

    def test_provider_secret_carries_no_variable_name(self) -> None:
        """密钥请求只带 id 与值——变量名由 Core 推导，Renderer 无从指定。"""
        request = self._decode("providers.set_secret", {"id": "a", "value": "sk-live"})
        self.assertEqual(request.payload, {"id": "a", "value": "sk-live"})

        for payload in (
            {"id": "a"},
            {"id": "a", "value": ""},
            {"id": "a", "value": "x" * 4097},
            # 绝不接受调用方指定写哪个环境变量
            {"id": "a", "value": "v", "name": "LOBSTER0_MODEL_API_KEY"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ProtocolError) as captured:
                    self._decode("providers.set_secret", payload)
                self.assertEqual(captured.exception.code, "invalid_provider_action")

    def test_encode_frame_is_one_utf8_json_line_and_rejects_nan(self) -> None:
        """输出必须是一行紧凑 UTF-8 JSON，且不能编码非标准数值。"""
        encoded = encode_frame(
            BridgeFrame(
                type="event.model_text_delta",
                payload={"turn_id": 9, "text": "你好"},
            )
        )

        self.assertTrue(encoded.endswith(b"\n"))
        self.assertEqual(encoded.count(b"\n"), 1)
        self.assertEqual(
            json.loads(encoded),
            {
                "v": 1,
                "type": "event.model_text_delta",
                "payload": {"turn_id": 9, "text": "你好"},
            },
        )

        with self.assertRaises(ProtocolError) as captured:
            encode_frame(BridgeFrame(type="event.model_usage", payload={"value": float("nan")}))
        self.assertEqual(captured.exception.code, "invalid_frame")


def _frame(request_type: str, payload: dict) -> bytes:
    """把类型与 payload 编成一条合法的 NDJSON 请求帧。"""
    return (
        json.dumps(
            {"v": 1, "id": "req-1", "type": request_type, "payload": payload},
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )


class AttachmentProtocolTest(unittest.TestCase):
    """附件请求的字段边界。"""

    def test_stage_requires_an_absolute_path_and_declared_type(self) -> None:
        """路径必须绝对：相对路径的含义取决于 Core 的 cwd，不可控。"""
        request = decode_request(
            _frame(
                "attachment.stage",
                {"path": "/tmp/note.txt", "declared_media_type": "text/plain"},
            )
        )

        self.assertEqual(request.payload["path"], "/tmp/note.txt")

        for payload in (
            {"path": "note.txt", "declared_media_type": "text/plain"},
            {"path": "", "declared_media_type": "text/plain"},
            {"path": "/tmp/a\x00b", "declared_media_type": "text/plain"},
            {"path": "/tmp/note.txt"},
            {"path": "/tmp/note.txt", "declared_media_type": "text/plain", "extra": 1},
        ):
            with self.assertRaises(ProtocolError) as raised:
                decode_request(_frame("attachment.stage", payload))
            self.assertEqual(raised.exception.code, "invalid_attachment")

    def test_turn_start_accepts_optional_attachment_ids(self) -> None:
        """附件字段可选：不带它的旧客户端必须继续可用。"""
        without = decode_request(
            _frame("turn.start", {"session_key": "s", "text": "hi"})
        )
        self.assertNotIn("attachment_ids", without.payload)

        with_ids = decode_request(
            _frame(
                "turn.start",
                {"session_key": "s", "text": "hi", "attachment_ids": ["art_" + "a" * 64]},
            )
        )
        self.assertEqual(len(with_ids.payload["attachment_ids"]), 1)

    def test_turn_start_refuses_malformed_attachment_ids(self) -> None:
        """id 形状固定，越界的一律拒绝，避免把任意字符串带进 Store 查询。"""
        for ids in (
            "not-a-list",
            [1],
            ["../escape"],
            ["art_" + "a" * 63],
            ["art_" + "A" * 64],
            [],
            ["art_" + "a" * 64] * 11,
        ):
            with self.assertRaises(ProtocolError) as raised:
                decode_request(
                    _frame(
                        "turn.start",
                        {"session_key": "s", "text": "hi", "attachment_ids": ids},
                    )
                )
            self.assertEqual(raised.exception.code, "invalid_turn", ids)


class ArtifactProtocolTest(unittest.TestCase):
    """产物查询与预览的字段边界。"""

    def test_list_requires_a_bounded_limit(self) -> None:
        """列表必须有界，避免一次把整个会话的产物读进内存。"""
        request = decode_request(
            _frame("artifacts.list", {"session_key": "s", "limit": 50})
        )
        self.assertEqual(request.payload["limit"], 50)

        for payload in (
            {"session_key": "s", "limit": 0},
            {"session_key": "s", "limit": 501},
            {"session_key": "s", "limit": True},
            {"session_key": "s"},
            {"session_key": "", "limit": 10},
            {"session_key": "s", "limit": 10, "extra": 1},
        ):
            with self.assertRaises(ProtocolError) as raised:
                decode_request(_frame("artifacts.list", payload))
            self.assertEqual(raised.exception.code, "invalid_artifact_query", payload)

    def test_preview_bounds_the_requested_bytes(self) -> None:
        """预览字节数有上限：Renderer 不能让 Core 读一个超大文件进内存。"""
        artifact_id = "art_" + "a" * 64
        request = decode_request(
            _frame("artifacts.preview", {"artifact_id": artifact_id, "max_bytes": 4096})
        )
        self.assertEqual(request.payload["artifact_id"], artifact_id)

        for payload in (
            {"artifact_id": artifact_id, "max_bytes": 0},
            {"artifact_id": artifact_id, "max_bytes": 10_000_000},
            {"artifact_id": "art_short", "max_bytes": 4096},
            {"artifact_id": "../escape", "max_bytes": 4096},
            {"artifact_id": artifact_id},
        ):
            with self.assertRaises(ProtocolError) as raised:
                decode_request(_frame("artifacts.preview", payload))
            self.assertEqual(raised.exception.code, "invalid_artifact_query", payload)

    def test_reveal_takes_only_an_artifact_id(self) -> None:
        """reveal 不接受任何路径——路径只能由 Core 从 id 解析。"""
        artifact_id = "art_" + "b" * 64
        request = decode_request(_frame("artifacts.reveal", {"artifact_id": artifact_id}))
        self.assertEqual(set(request.payload), {"artifact_id"})

        with self.assertRaises(ProtocolError) as raised:
            decode_request(
                _frame("artifacts.reveal", {"artifact_id": artifact_id, "path": "/tmp"})
            )
        self.assertEqual(raised.exception.code, "invalid_artifact_query")


if __name__ == "__main__":
    unittest.main()
