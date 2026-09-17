"""TRV 小程序页面的状态读取与模式切换。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.adb import Adb
from core.errors import AutomationError
from core.ui_snapshot import Node, UiSnapshot
from core.waits import wait_until

PERCENT_RE = re.compile(r"^(\d{1,3})%$")
CELSIUS_RE = re.compile(r"^(\d+(?:\.\d+)?)℃$")
NUMBER_PART_RE = re.compile(r"^\d+$|^\.$")


@dataclass(frozen=True)
class HeaderStatus:
    """PID 顶部状态栏中的模式、阀门开度和电量节点。"""

    mode_node: Node
    valve_node: Node
    battery_node: Node


@dataclass(frozen=True)
class TrvStatus:
    """从 TRV 页面读取到的当前模式、温度和阀门状态。"""

    mode: str
    valve_percent: int
    battery_percent: int
    room_temperature: float
    setpoint: float | None


def read_header(snapshot: UiSnapshot) -> HeaderStatus:
    """结构化解析 PID 顶部栏，允许当前模式为自定义或其他预设。"""
    headers: list[HeaderStatus] = []
    all_percentages = [node for node in snapshot.nodes() if PERCENT_RE.fullmatch(node.text)]
    for container in snapshot.nodes():
        if container.class_name != "android.widget.HorizontalScrollView":
            continue
        text_nodes = [node for node in container.descendants() if node.text]
        percentages = sorted(
            (node for node in text_nodes if PERCENT_RE.fullmatch(node.text)),
            key=lambda node: node.bounds[0],
        )
        if len(percentages) < 2:
            continue
        mode_nodes = [
            node
            for node in text_nodes
            if not PERCENT_RE.fullmatch(node.text) and node.bounds[2] <= percentages[0].bounds[0]
        ]
        if mode_nodes:
            headers.append(
                HeaderStatus(
                    mode_node=min(mode_nodes, key=lambda node: (node.bounds[0], node.bounds[1])),
                    valve_node=percentages[0],
                    battery_node=percentages[1],
                )
            )
    if headers:
        return min(headers, key=lambda header: header.mode_node.bounds[1])
    if len(all_percentages) == 1:
        raise AutomationError("顶部仅识别到一个百分比，疑似处于 on/off 模式而不是 PID 模式。")
    if all_percentages:
        raise AutomationError("识别到百分比，但未找到 PID 的模式、开度、电量状态栏。")
    raise AutomationError("未识别到 PID 顶部状态栏；页面可能尚未加载完成。")


def read_large_setpoint(snapshot: UiSnapshot) -> float | None:
    """还原中部大字号设温，不依赖固定的屏幕绝对坐标。"""
    parts = [node for node in snapshot.nodes() if NUMBER_PART_RE.fullmatch(node.text)]
    groups: list[list[Node]] = []
    for part in sorted(parts, key=lambda node: (node.center[1], node.bounds[0])):
        for group in groups:
            tolerance = max(8, min(group[0].height, part.height) // 3)
            if abs(group[0].center[1] - part.center[1]) <= tolerance:
                group.append(part)
                break
        else:
            groups.append([part])

    candidates: list[tuple[float, int]] = []
    for group in groups:
        text = "".join(node.text for node in sorted(group, key=lambda node: node.bounds[0]))
        try:
            value = float(text)
        except ValueError:
            continue
        if 0 <= value <= 50:
            candidates.append((value, sum(node.width * node.height for node in group)))
    return max(candidates, key=lambda item: item[1])[0] if candidates else None


def menu_target_node(
    snapshot: UiSnapshot,
    target_mode: str,
    before_menu: set[tuple[str, tuple[int, int, int, int], str]],
) -> Node | None:
    """定位弹层中新出现的目标模式，避免误点页面里已有的同名文字。"""
    matches = [node for node in snapshot.nodes() if target_mode in node.text]
    newly_visible = [node for node in matches if node.identity() not in before_menu]
    candidates = newly_visible or [node for node in matches if node.text != target_mode]
    if not candidates:
        return None
    return max(candidates, key=lambda node: (len(node.text), node.bounds[1]))


class TrvPanel:
    """封装 TRV 小程序页面的页面判定、状态读取和模式选择。"""

    def __init__(self, adb: Adb, device_name: str, timeout: float, poll_interval: float) -> None:
        """保存 TRV 页面识别和等待所需的运行信息。"""
        self.adb = adb
        self.device_name = device_name
        self.timeout = timeout
        self.poll_interval = poll_interval

    def is_target_page(self, snapshot: UiSnapshot) -> bool:
        """判断快照是否为配置指定设备的 TRV 页面。"""
        texts = {node.text for node in snapshot.nodes()}
        return self.device_name in texts and "模式" in texts

    def read_status(self, snapshot: UiSnapshot) -> TrvStatus:
        """读取页面中的模式、阀门开度、电量、室温和设温。"""
        header = read_header(snapshot)
        valve_match = PERCENT_RE.fullmatch(header.valve_node.text)
        battery_match = PERCENT_RE.fullmatch(header.battery_node.text)
        assert valve_match is not None and battery_match is not None
        temperatures = [
            (node, float(match.group(1)))
            for node in snapshot.nodes()
            if (match := CELSIUS_RE.fullmatch(node.text))
        ]
        if not temperatures:
            raise AutomationError("未读取到带 ℃ 标识的室温。")
        return TrvStatus(
            mode=header.mode_node.text,
            valve_percent=int(valve_match.group(1)),
            battery_percent=int(battery_match.group(1)),
            room_temperature=min(temperatures, key=lambda item: item[0].bounds[1])[1],
            setpoint=read_large_setpoint(snapshot),
        )

    def wait_for_status(self, description: str) -> TrvStatus:
        """等待 PID 状态栏和大字号设温同时完成加载。"""
        return wait_until(
            lambda: self.read_status(self.adb.dump_ui()),
            lambda status: status.setpoint is not None,
            self.timeout,
            self.poll_interval,
            description,
        )

    def select_mode(self, target_mode: str) -> TrvStatus:
        """打开模式弹层，选择目标预设，并等待目标模式和设温生效。"""
        snapshot = self.adb.dump_ui()
        current = self.read_status(snapshot)
        if current.mode == target_mode and current.setpoint is not None:
            return current

        header = read_header(snapshot)
        before_menu = {node.identity() for node in snapshot.nodes()}
        self.adb.tap(*header.mode_node.center)
        menu = wait_until(
            self.adb.dump_ui,
            lambda item: menu_target_node(item, target_mode, before_menu) is not None,
            self.timeout,
            self.poll_interval,
            f"模式菜单中的 {target_mode}",
        )
        target = menu_target_node(menu, target_mode, before_menu)
        assert target is not None
        self.adb.tap(*target.center)
        return wait_until(
            lambda: self.read_status(self.adb.dump_ui()),
            lambda status: status.mode == target_mode and status.setpoint is not None,
            self.timeout,
            self.poll_interval,
            f"{target_mode} 模式生效",
        )
