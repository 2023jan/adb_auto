"""智能生活首页及设备入口的通用操作。"""

from __future__ import annotations

import time
from collections.abc import Callable

from core.adb import Adb
from core.errors import AutomationError
from core.ui_snapshot import UiSnapshot, find_exact
from core.waits import wait_until


class SmartLifeHome:
    """负责将智能生活恢复到指定设备的页面。"""

    def __init__(
        self,
        adb: Adb,
        package: str,
        launch_activity: str,
        timeout: float,
        poll_interval: float,
        device_search_max_swipes: int,
    ) -> None:
        """保存页面恢复所需的 App 信息和轮询配置。"""
        self.adb = adb
        self.package = package
        self.launch_activity = launch_activity
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.device_search_max_swipes = device_search_max_swipes

    def ensure_device_page(
        self,
        device_name: str,
        page_ready: Callable[[UiSnapshot], bool],
    ) -> bool:
        """进入设备页；若本来已在目标页则返回 ``True``。"""
        try:
            if page_ready(self.adb.dump_ui()):
                return True
        except AutomationError:
            # 无法读取当前页时仍可重新启动 App 并从首页恢复。
            pass

        self.adb.start_app(self.package, self.launch_activity)
        deadline = time.monotonic() + self.timeout
        swipes = 0
        last_error: AutomationError | None = None
        while time.monotonic() < deadline:
            try:
                snapshot = self.adb.dump_ui()
                if page_ready(snapshot):
                    return False
                device_node = find_exact(snapshot, device_name)
                if device_node:
                    self.adb.tap(*device_node.center)
                    wait_until(
                        self.adb.dump_ui,
                        page_ready,
                        self.timeout,
                        self.poll_interval,
                        f"设备 {device_name} 的页面",
                    )
                    return False
                if swipes < self.device_search_max_swipes:
                    self._swipe_up(snapshot)
                    swipes += 1
            except AutomationError as exc:
                last_error = exc
            time.sleep(self.poll_interval)
        raise AutomationError(
            f"打开智能生活后未找到设备 {device_name}；已向上滑动 {swipes} 次。最后错误：{last_error}"
        )

    def _swipe_up(self, snapshot: UiSnapshot) -> None:
        """根据当前屏幕大小在设备列表区域向上滑动一次。"""
        if not snapshot.roots:
            raise AutomationError("无法确定当前屏幕范围，不能滑动设备列表。")
        left, top, right, bottom = snapshot.roots[0].bounds
        height = bottom - top
        center_x = (left + right) // 2
        # 从列表下方滑到中部，避免触及底部导航栏和顶部房间标签。
        start = (center_x, top + int(height * 0.80))
        end = (center_x, top + int(height * 0.32))
        self.adb.swipe(start, end)
