"""通过 ADB 控制 Android 手机，并读取 UIAutomator XML。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import AutomationError
from .ui_snapshot import UiSnapshot, parse_snapshot


class Adb:
    """封装 ADB 的启动 App、点击、UI 读取和截图操作。"""

    def __init__(self, serial: str | None) -> None:
        """使用可选设备序列号构造 ADB 命令前缀。"""
        self.prefix = ["adb"] + (["-s", serial] if serial else [])
        self.last_ui_xml: str | None = None

    def run(self, *args: str, binary: bool = False) -> str | bytes:
        """执行 ADB 子命令，并将命令错误转换为统一异常。"""
        command = self.prefix + list(args)
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise AutomationError(f"ADB 命令失败：{' '.join(command)}\n{stderr}")
        return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")

    def tap(self, x: int, y: int) -> None:
        """点击由 UI 节点实时定位得到的坐标。"""
        self.run("shell", "input", "tap", str(x), str(y))

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration_ms: int = 450) -> None:
        """执行一次屏幕滑动；页面对象应根据当前 UI 快照给出相对位置。"""
        self.run(
            "shell",
            "input",
            "swipe",
            str(start[0]),
            str(start[1]),
            str(end[0]),
            str(end[1]),
            str(duration_ms),
        )

    def start_app(self, package: str, activity: str) -> None:
        """将配置指定的 Android App 启动到前台。"""
        self.run("shell", "am", "start", "-n", f"{package}/{activity}")

    def require_unlocked(self) -> None:
        """在业务操作前阻止锁屏手机，避免把系统遮罩误报为 App 页面异常。"""
        policy = self.run("shell", "dumpsys", "window", "policy")
        assert isinstance(policy, str)
        if "mIsShowing=true" in policy:
            raise AutomationError("手机当前已锁屏。请保持屏幕唤醒、解锁并停留在可操作状态后再执行。")

    def dump_ui(self) -> UiSnapshot:
        """导出当前 UIAutomator XML，并移除 Honor 可能附加的尾部文字。"""
        output = self.run("exec-out", "uiautomator", "dump", "--compressed", "/dev/tty")
        assert isinstance(output, str)
        start, end = output.find("<hierarchy"), output.rfind("</hierarchy>")
        if start < 0 or end < 0:
            raise AutomationError("未读取到完整 Android UI XML。")
        self.last_ui_xml = output[start : end + len("</hierarchy>")]
        return parse_snapshot(self.last_ui_xml)

    def screenshot(self, destination: Path) -> None:
        """将当前手机截图写入目标文件。"""
        screenshot = self.run("exec-out", "screencap", "-p", binary=True)
        assert isinstance(screenshot, bytes)
        destination.write_bytes(screenshot)

    def save_last_ui_xml(self, destination: Path) -> None:
        """保存最近一次成功读取到的 UI XML。"""
        if self.last_ui_xml is None:
            raise AutomationError("尚未读取到可保存的 UI XML。")
        destination.write_text(self.last_ui_xml, encoding="utf-8")
