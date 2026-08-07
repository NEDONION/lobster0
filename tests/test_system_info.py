"""``system_info`` 的平台字段、隐私和参数边界测试。"""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from miniclaw.tools.base import ToolContext, ToolValidationError
from miniclaw.tools.system import SystemInfoTool, _mac_hardware


class SystemInfoToolTest(unittest.IsolatedAsyncioTestCase):
    """验证系统信息来自真实平台边界且只返回允许字段。"""

    def setUp(self) -> None:
        """创建不含任何真实用户目录信息的 ToolContext。"""
        self.context = ToolContext(
            user_id=1,
            session_id=2,
            turn_id=3,
            state_home=Path("/state"),
            workspace=Path("/workspace"),
            read_only_roots=(),
        )

    async def test_returns_whitelisted_sections_without_device_identifiers(self) -> None:
        """macOS 结果必须包含可用配置，同时排除设备和用户身份字段。"""
        tool = SystemInfoTool()
        with (
            mock.patch("miniclaw.tools.system.platform.system", return_value="Darwin"),
            mock.patch(
                "miniclaw.tools.system.platform.mac_ver",
                return_value=("15.1", ("", "", ""), "arm64"),
            ),
            mock.patch("miniclaw.tools.system.platform.machine", return_value="arm64"),
            mock.patch("miniclaw.tools.system.os.cpu_count", return_value=10),
            mock.patch(
                "miniclaw.tools.system._mac_hardware",
                return_value={
                    "chip": "Apple M4",
                    "memory_bytes": 17_179_869_184,
                    "gpus": ["Apple M4"],
                },
            ),
            mock.patch(
                "miniclaw.tools.system.shutil.disk_usage",
                return_value=SimpleNamespace(total=1000, used=400, free=600),
            ),
        ):
            arguments = tool.validate({})
            result = await tool.execute(self.context, arguments)

        self.assertTrue(result.ok)
        self.assertIsInstance(result.data, dict)
        assert isinstance(result.data, dict)
        cpu = result.data["cpu"]
        memory = result.data["memory"]
        self.assertIsInstance(cpu, dict)
        self.assertIsInstance(memory, dict)
        assert isinstance(cpu, dict)
        assert isinstance(memory, dict)
        self.assertEqual(cpu["model"], "Apple M4")
        self.assertEqual(cpu["logical_cores"], 10)
        self.assertEqual(memory["total_bytes"], 17_179_869_184)
        serialized = json.dumps(result.data).lower()
        for forbidden in (
            "serial",
            "uuid",
            "hostname",
            "username",
            "mac_address",
            "environment",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_validate_rejects_unknown_sections_and_parameters(self) -> None:
        """模型不能借 Tool 参数读取序列号或执行任意命令。"""
        tool = SystemInfoTool()

        for arguments in ({"sections": ["serial"]}, {"command": "env"}):
            with self.subTest(arguments=arguments), self.assertRaises(ToolValidationError):
                tool.validate(arguments)

    async def test_platform_collector_failure_marks_sections_unavailable(self) -> None:
        """单个平台查询失败时仍返回成功，并明确哪些分区不可用。"""
        tool = SystemInfoTool()
        with (
            mock.patch("miniclaw.tools.system.platform.system", return_value="Darwin"),
            mock.patch("miniclaw.tools.system._mac_hardware", side_effect=OSError("blocked")),
        ):
            result = await tool.execute(
                self.context,
                tool.validate({"sections": ["cpu", "gpu"]}),
            )

        self.assertTrue(result.ok)
        self.assertIsInstance(result.data, dict)
        assert isinstance(result.data, dict)
        self.assertEqual(result.data["unavailable_sections"], ["cpu", "gpu"])

    def test_mac_collector_uses_fixed_commands_and_whitelists_output(self) -> None:
        """macOS 命令参数必须写死，解析结果不能带出序列号。"""
        profiler_output = json.dumps(
            {
                "SPHardwareDataType": [
                    {
                        "chip_type": "Apple M4",
                        "serial_number": "DO-NOT-RETURN",
                        "platform_UUID": "DO-NOT-RETURN",
                    }
                ],
                "SPDisplaysDataType": [{"sppci_model": "Apple M4 GPU"}],
            }
        )
        with mock.patch("miniclaw.tools.system.subprocess.run") as run:
            run.side_effect = [
                SimpleNamespace(returncode=0, stdout=profiler_output),
                SimpleNamespace(returncode=0, stdout="17179869184\n"),
            ]

            result = _mac_hardware()

        self.assertEqual(
            result,
            {
                "chip": "Apple M4",
                "memory_bytes": 17_179_869_184,
                "gpus": ["Apple M4 GPU"],
            },
        )
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                [
                    "/usr/sbin/system_profiler",
                    "SPHardwareDataType",
                    "SPDisplaysDataType",
                    "-json",
                ],
                ["/usr/sbin/sysctl", "-n", "hw.memsize"],
            ],
        )

    async def test_missing_linux_cpu_model_is_marked_unavailable(self) -> None:
        """Linux 读取不到 CPU 型号时不能只返回 unknown 而假装分区完整。"""
        tool = SystemInfoTool()
        with (
            mock.patch("miniclaw.tools.system.platform.system", return_value="Linux"),
            mock.patch("miniclaw.tools.system._linux_cpu_model", return_value=None),
            mock.patch("miniclaw.tools.system.os.cpu_count", return_value=4),
        ):
            result = await tool.execute(
                self.context,
                tool.validate({"sections": ["cpu"]}),
            )

        self.assertTrue(result.ok)
        self.assertIsInstance(result.data, dict)
        assert isinstance(result.data, dict)
        self.assertEqual(result.data["unavailable_sections"], ["cpu"])


if __name__ == "__main__":
    unittest.main()
