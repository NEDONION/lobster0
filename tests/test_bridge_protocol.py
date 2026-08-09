"""Python Core 与 pi-tui 之间 NDJSON protocol v1 的契约测试。"""

import json
import unittest

from miniclaw.bridge.protocol import (
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
            b'"payload":{"session_key":"default","text":"\xe4\xbd\xa0\xe5\xa5\xbd\\nMiniClaw"}}\n'
        )

        self.assertEqual(request.version, 1)
        self.assertEqual(request.request_id, "req-1")
        self.assertEqual(request.type, "turn.start")
        self.assertEqual(request.payload, {"session_key": "default", "text": "你好\nMiniClaw"})

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


if __name__ == "__main__":
    unittest.main()
