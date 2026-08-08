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
        )

        for frame, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                raw = json.dumps(frame).encode("utf-8") + b"\n"
                with self.assertRaises(ProtocolError) as captured:
                    decode_request(raw)
                self.assertEqual(captured.exception.code, expected_code)

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
