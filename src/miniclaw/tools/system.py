"""读取当前机器的非敏感系统配置。"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
from typing import Any

from miniclaw.providers.base import JsonValue
from miniclaw.tools.base import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)

_SECTIONS = ("os", "cpu", "memory", "storage", "gpu")


class SystemInfoTool:
    """返回操作系统、处理器、内存、磁盘和显卡的安全摘要。"""

    definition = ToolDefinition(
        name="system_info",
        description=(
            "Read the current machine's real operating system, CPU, memory, storage, "
            "and GPU information. Use this instead of saying you cannot inspect the computer."
        ),
        parameters={
            "type": "object",
            "properties": {
                "sections": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(_SECTIONS)},
                    "minItems": 1,
                    "uniqueItems": True,
                }
            },
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """只接受公开 Schema 中的安全分区。"""
        unexpected = set(arguments) - {"sections"}
        if unexpected:
            raise ToolValidationError("system_info only accepts the 'sections' parameter")

        sections = arguments.get("sections", list(_SECTIONS))
        if not isinstance(sections, list) or not sections:
            raise ToolValidationError("sections must be a non-empty list")
        if any(not isinstance(section, str) or section not in _SECTIONS for section in sections):
            raise ToolValidationError(f"sections must contain only: {', '.join(_SECTIONS)}")
        if len(set(sections)) != len(sections):
            raise ToolValidationError("sections must not contain duplicates")
        return {"sections": sections}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """读取本机数据；身份字段永远不进入返回值。"""
        del context
        sections = arguments["sections"]
        assert isinstance(sections, list)
        data = await asyncio.to_thread(_collect_system_info, sections)
        return ToolResult.success(data)


def _collect_system_info(sections: list[JsonValue]) -> dict[str, JsonValue]:
    """按白名单分区收集数据，并把局部失败降级为 unavailable。"""
    requested = [section for section in sections if isinstance(section, str)]
    system = platform.system()
    hardware: dict[str, Any] = {}
    hardware_available = True
    if system == "Darwin":
        try:
            hardware = _mac_hardware()
        except Exception:  # noqa: BLE001 - 平台查询失败不能拖垮整个 Tool
            hardware_available = False

    data: dict[str, JsonValue] = {}
    unavailable: list[JsonValue] = []

    if "os" in requested:
        data["os"] = {
            "name": "macOS" if system == "Darwin" else system or "unknown",
            "version": platform.mac_ver()[0] if system == "Darwin" else platform.release(),
            "architecture": platform.machine() or "unknown",
        }
    if "cpu" in requested:
        model = hardware.get("chip") if system == "Darwin" else _linux_cpu_model()
        data["cpu"] = {"model": model or "unknown", "logical_cores": os.cpu_count()}
        if not model:
            unavailable.append("cpu")
    if "memory" in requested:
        total = hardware.get("memory_bytes") if system == "Darwin" else _physical_memory_bytes()
        data["memory"] = {"total_bytes": total}
        if total is None:
            unavailable.append("memory")
    if "storage" in requested:
        try:
            usage = shutil.disk_usage("/")
            data["storage"] = [
                {
                    "mount": "/",
                    "total_bytes": usage.total,
                    "free_bytes": usage.free,
                }
            ]
        except OSError:
            data["storage"] = []
            unavailable.append("storage")
    if "gpu" in requested:
        models = hardware.get("gpus", []) if system == "Darwin" else []
        data["gpu"] = [{"model": model} for model in models]
        if system != "Darwin" or not hardware_available or "gpus" not in hardware:
            unavailable.append("gpu")

    data["unavailable_sections"] = unavailable
    return data


def _mac_hardware() -> dict[str, Any]:
    """运行固定 macOS 查询，只提取 CPU、内存和 GPU 白名单字段。"""
    result: dict[str, Any] = {}
    profiler = _run_fixed(
        [
            "/usr/sbin/system_profiler",
            "SPHardwareDataType",
            "SPDisplaysDataType",
            "-json",
        ]
    )
    if profiler is not None:
        try:
            payload = json.loads(profiler.stdout)
        except json.JSONDecodeError:
            payload = {}
        hardware = payload.get("SPHardwareDataType", []) if isinstance(payload, dict) else []
        displays = payload.get("SPDisplaysDataType", []) if isinstance(payload, dict) else []
        if hardware and isinstance(hardware[0], dict):
            chip = hardware[0].get("chip_type")
            if isinstance(chip, str) and chip:
                result["chip"] = chip
        if isinstance(displays, list):
            result["gpus"] = [
                model
                for display in displays
                if isinstance(display, dict)
                and isinstance((model := display.get("sppci_model")), str)
                and model
            ]

    memory = _run_fixed(["/usr/sbin/sysctl", "-n", "hw.memsize"])
    if memory is not None:
        try:
            result["memory_bytes"] = int(memory.stdout.strip())
        except ValueError:
            pass
    return result


def _run_fixed(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    """运行代码内写死的系统查询，模型永远不能提供 argv。"""
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed if completed.returncode == 0 else None


def _linux_cpu_model() -> str | None:
    """从 Linux 标准 procfs 读取第一个 CPU 型号。"""
    if platform.system() != "Linux":
        return platform.processor() or None
    try:
        for line in open("/proc/cpuinfo", encoding="utf-8"):  # noqa: SIM115
            if line.lower().startswith("model name"):
                return line.partition(":")[2].strip() or None
    except OSError:
        return None
    return None


def _physical_memory_bytes() -> int | None:
    """使用 POSIX sysconf 读取物理内存总量。"""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError):
        return None
