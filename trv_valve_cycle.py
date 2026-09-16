#!/usr/bin/env python3
"""在 PID 模式下循环切换涂鸦 TRV，并统计已确认的阀门动作。

手机需保持解锁并已登录智能生活。只有当设备上报的 PID 阀门开度按预期方向
变化后，脚本才将该次模式切换计为一次实际动作。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


PACKAGE = "com.tuya.smartlifeiot"
LAUNCH_ACTIVITY = "com.smart.ThingSplashActivity"
PERCENT_RE = re.compile(r"^(\d{1,3})%$")
CELSIUS_RE = re.compile(r"^(\d+(?:\.\d+)?)℃$")
NUMBER_PART_RE = re.compile(r"^\d+$|^\.$")


class TrvError(RuntimeError):
    """出现不适合继续控制设备的状态时抛出的安全异常。"""


@dataclass(frozen=True)
class Node:
    """Android UI 层级中的一个节点，包含文字和它在屏幕上的矩形范围。"""

    text: str
    bounds: tuple[int, int, int, int]
    class_name: str

    @property
    def center(self) -> tuple[int, int]:
        """返回节点中心坐标，作为点击该元素的安全位置。"""
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)

    @property
    def width(self) -> int:
        """返回节点宽度，单位为 Android 屏幕像素。"""
        return self.bounds[2] - self.bounds[0]

    @property
    def height(self) -> int:
        """返回节点高度，单位为 Android 屏幕像素。"""
        return self.bounds[3] - self.bounds[1]


@dataclass(frozen=True)
class Status:
    """从一次 Android UI 快照中读取到的 TRV 状态。"""

    mode: str
    valve_percent: int
    room_temperature: float
    setpoint: float | None


class Adb:
    """封装 ADB 的最小操作集，用于读取 UI 和发送点击。"""

    def __init__(self, serial: str | None) -> None:
        """构造 ADB 命令前缀；多设备时可指定目标序列号。"""
        self.prefix = ["adb"] + (["-s", serial] if serial else [])

    def run(self, *args: str, binary: bool = False) -> str | bytes:
        """执行 ADB 子命令，并将命令失败转换为可安全停止的脚本异常。"""
        command = self.prefix + list(args)
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise TrvError(f"ADB command failed: {' '.join(command)}\n{stderr}")
        if binary:
            return completed.stdout
        return completed.stdout.decode("utf-8", errors="replace")

    def tap(self, x: int, y: int) -> None:
        """点击一个已通过 UI 定位确认过的 Android 屏幕坐标。"""
        self.run("shell", "input", "tap", str(x), str(y))

    def start_app(self) -> None:
        """目标设备页未打开时，将智能生活切回前台。"""
        self.run(
            "shell",
            "am",
            "start",
            "-n",
            f"{PACKAGE}/{LAUNCH_ACTIVITY}",
        )

    def dump_ui(self) -> list[Node]:
        """读取 Android 无障碍 UI 层级，并转换为统一的节点列表。"""
        # 部分 Honor 系统会在 XML 后附加状态文字，先截取完整 hierarchy，
        # 再交给结构化 XML 解析器，避免用字符串规则解析 UI。
        output = self.run("exec-out", "uiautomator", "dump", "--compressed", "/dev/tty")
        start = output.find("<hierarchy")
        end = output.rfind("</hierarchy>")
        if start < 0 or end < 0:
            raise TrvError("Could not read the Android UI hierarchy.")
        xml = output[start : end + len("</hierarchy>")]
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise TrvError(f"Could not parse Android UI hierarchy: {exc}") from exc

        nodes: list[Node] = []
        for element in root.iter("node"):
            bounds = parse_bounds(element.attrib.get("bounds", ""))
            if bounds is None:
                continue
            nodes.append(
                Node(
                    text=element.attrib.get("text", "").strip(),
                    bounds=bounds,
                    class_name=element.attrib.get("class", ""),
                )
            )
        return nodes

    def screenshot(self, destination: Path) -> None:
        """保存无损截图，作为失败或完成时的测试证据。"""
        data = self.run("exec-out", "screencap", "-p", binary=True)
        destination.write_bytes(data)


def parse_bounds(value: str) -> tuple[int, int, int, int] | None:
    """将 Android 的 '[左,上][右,下]' bounds 属性转换为整数坐标。"""
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value)
    if not match:
        return None
    return tuple(int(group) for group in match.groups())  # type: ignore[return-value]


class TrvCycle:
    """协调模式选择、PID 安全校验、阀门动作计数和证据留存。"""

    def __init__(
        self,
        adb: Adb,
        device_name: str,
        timeout: float,
        poll_interval: float,
        run_dir: Path,
        close_mode: str,
        open_mode: str,
    ) -> None:
        """保存运行配置，并拒绝把开阀和关阀配置成相同模式。"""
        self.adb = adb
        self.device_name = device_name
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.run_dir = run_dir
        self.log_path = run_dir / "events.jsonl"
        self.close_mode = close_mode
        self.open_mode = open_mode
        self.mode_names = {close_mode, open_mode}
        if len(self.mode_names) != 2:
            raise TrvError("--close-mode and --open-mode must name two different modes.")

    def log(self, event: str, **fields: object) -> None:
        """追加一条 JSONL 事件日志，同时原样打印到终端。"""
        record = {"time": datetime.now().isoformat(timespec="seconds"), "event": event, **fields}
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def save_screenshot(self, label: str) -> Path:
        """用带时间戳的文件名保存当前手机截图。"""
        safe_label = re.sub(r"[^a-zA-Z0-9_.-]+", "-", label)
        filename = f"{datetime.now():%H%M%S}-{safe_label}.png"
        destination = self.run_dir / filename
        self.adb.screenshot(destination)
        return destination

    def is_target_page(self, nodes: Iterable[Node]) -> bool:
        """判断快照是否为指定设备的 TRV 控制页。"""
        texts = {node.text for node in nodes}
        return self.device_name in texts and "模式" in texts and any(
            PERCENT_RE.fullmatch(node.text) for node in nodes
        )

    def wait_until(self, predicate, description: str) -> list[Node]:
        """轮询 UI，直到条件成立或达到配置的超时时间。"""
        deadline = time.monotonic() + self.timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                nodes = self.adb.dump_ui()
                if predicate(nodes):
                    return nodes
            except TrvError as exc:
                last_error = exc
            time.sleep(self.poll_interval)
        detail = f" ({last_error})" if last_error else ""
        raise TrvError(f"Timed out waiting for {description}{detail}")

    def ensure_target_page(self) -> list[Node]:
        """复用已打开的设备页，或启动智能生活后进入指定设备卡片。"""
        nodes = self.adb.dump_ui()
        if self.is_target_page(nodes):
            self.log("using_current_device_page", device=self.device_name)
            return nodes

        self.log("opening_smart_life", device=self.device_name)
        self.adb.start_app()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            nodes = self.adb.dump_ui()
            if self.is_target_page(nodes):
                return nodes
            device_node = find_exact(nodes, self.device_name)
            if device_node:
                self.log("opening_device", device=self.device_name, bounds=device_node.bounds)
                self.adb.tap(*device_node.center)
                return self.wait_until(self.is_target_page, f"TRV page for {self.device_name}")
            time.sleep(self.poll_interval)
        raise TrvError(f"Could not find device {self.device_name} after opening Smart Life.")

    def current_mode_node(self, nodes: Iterable[Node]) -> Node:
        """定位当前模式胶囊，避开页面其他位置重复出现的同名模式。"""
        candidates = [node for node in nodes if node.text in self.mode_names]
        if not candidates:
            raise TrvError("Could not identify the current TRV preset mode.")
        # 当前模式胶囊始终是同名文字中最靠上的一个；模式名也可能出现在
        # 中部选择器和下方设置卡片中，不能按第一次字符串匹配直接点击。
        return min(candidates, key=lambda node: (node.bounds[1], node.bounds[0]))

    def read_status(self, nodes: Iterable[Node]) -> Status:
        """提取当前模式、阀门开度、室温和大字号设温。"""
        node_list = list(nodes)
        mode_node = self.current_mode_node(node_list)
        mode = mode_node.text

        percent_nodes = [
            node
            for node in node_list
            if PERCENT_RE.fullmatch(node.text)
            and abs(node.center[1] - mode_node.center[1]) <= 120
            # PID 阀门开度是模式胶囊右侧第一枚状态芯片；后面的百分比是电量。
            and mode_node.bounds[2] < node.bounds[0] < mode_node.bounds[2] + 450
        ]
        if not percent_nodes:
            raise TrvError(
                "No PID valve-opening percentage is visible. Confirm the TRV is in PID mode, not on/off mode."
            )
        valve_node = min(percent_nodes, key=lambda node: node.bounds[0])
        valve_match = PERCENT_RE.fullmatch(valve_node.text)
        assert valve_match is not None
        valve_percent = int(valve_match.group(1))
        if valve_percent > 100:
            raise TrvError(f"Invalid valve opening reported: {valve_node.text}")

        temperatures = []
        for node in node_list:
            match = CELSIUS_RE.fullmatch(node.text)
            if match:
                temperatures.append((node, float(match.group(1))))
        if not temperatures:
            raise TrvError("Could not read the current room temperature from the TRV page.")
        room_temperature = min(temperatures, key=lambda item: item[0].bounds[1])[1]

        return Status(
            mode=mode,
            valve_percent=valve_percent,
            room_temperature=room_temperature,
            setpoint=read_large_setpoint(node_list),
        )

    def select_mode(self, target_mode: str) -> Status:
        """从模式底部弹层中选择预设，并等待目标模式完整渲染。"""
        nodes = self.adb.dump_ui()
        current = self.read_status(nodes)
        if current.mode == target_mode and current.setpoint is not None:
            return current

        mode_node = self.current_mode_node(nodes)
        self.log("opening_mode_menu", current_mode=current.mode, target_mode=target_mode)
        self.adb.tap(*mode_node.center)

        def target_in_menu(menu_nodes: list[Node]) -> bool:
            """判断请求的预设是否已出现在底部模式弹层。"""
            return menu_target_node(menu_nodes, target_mode) is not None

        menu_nodes = self.wait_until(target_in_menu, f"{target_mode} item in the mode menu")
        target_node = menu_target_node(menu_nodes, target_mode)
        assert target_node is not None
        self.log("selecting_mode", target_mode=target_mode, bounds=target_node.bounds)
        self.adb.tap(*target_node.center)

        def mode_applied(new_nodes: list[Node]) -> bool:
            """确认目标模式名称和对应的大字号设温都已经可见。"""
            try:
                status = self.read_status(new_nodes)
            except TrvError:
                return False
            return status.mode == target_mode and status.setpoint is not None

        applied_nodes = self.wait_until(mode_applied, f"{target_mode} mode to apply")
        return self.read_status(applied_nodes)

    def validate_target_direction(self, status: Status, direction: str, target_mode: str) -> None:
        """确认目标设温与室温关系能让 PID 开度向预期方向变化。"""
        if status.setpoint is None:
            raise TrvError(f"Could not read the setpoint after selecting {target_mode}.")
        if direction == "decrease" and status.setpoint >= status.room_temperature:
            raise TrvError(
                f"{target_mode} is {status.setpoint:.1f}℃ while room temperature is {status.room_temperature:.1f}℃. "
                "The target must be lower than room temperature to close the PID valve."
            )
        if direction == "increase" and status.setpoint <= status.room_temperature:
            raise TrvError(
                f"{target_mode} is {status.setpoint:.1f}℃ while room temperature is {status.room_temperature:.1f}℃. "
                "The target must be higher than room temperature to open the PID valve."
            )

    def wait_for_valve_change(self, baseline: int, direction: str, action_number: int) -> Status:
        """轮询设备上报开度，直到相对基线按预期方向发生变化。"""
        deadline = time.monotonic() + self.timeout
        latest: Status | None = None
        while time.monotonic() < deadline:
            try:
                latest = self.read_status(self.adb.dump_ui())
                # 点击模式本身不代表阀门已动作，只有设备上报的开度按预期方向
                # 发生变化后，才将本次操作计入测试次数。
                changed = (
                    direction == "increase" and latest.valve_percent > baseline
                ) or (
                    direction == "decrease" and latest.valve_percent < baseline
                )
                if changed:
                    elapsed = round(self.timeout - (deadline - time.monotonic()), 2)
                    self.log(
                        "valve_motion_confirmed",
                        action=action_number,
                        direction=direction,
                        before_percent=baseline,
                        after_percent=latest.valve_percent,
                        mode=latest.mode,
                        room_temperature=latest.room_temperature,
                        elapsed_seconds=elapsed,
                    )
                    return latest
            except TrvError as exc:
                self.log("state_read_retry", action=action_number, error=str(exc))
            time.sleep(self.poll_interval)

        screenshot = self.save_screenshot(f"action-{action_number}-no-valve-change")
        last_percent = latest.valve_percent if latest else None
        raise TrvError(
            f"Action {action_number}: valve did not {direction} from {baseline}% within {self.timeout:.0f}s "
            f"(last reported {last_percent}%). Screenshot: {screenshot}"
        )

    def run(self, moves: int) -> None:
        """执行指定数量、且每次均已确认开度变化的 PID 阀门动作。"""
        nodes = self.ensure_target_page()
        status = self.read_status(nodes)
        self.log(
            "run_started",
            moves_requested=moves,
            initial_mode=status.mode,
            initial_valve_percent=status.valve_percent,
            room_temperature=status.room_temperature,
            setpoint=status.setpoint,
            timeout_seconds=self.timeout,
            close_mode=self.close_mode,
            open_mode=self.open_mode,
        )

        for action_number in range(1, moves + 1):
            baseline = status.valve_percent
            # 根据当前开度选择相反端点。即使脚本从人工测试的中间状态启动，
            # 首次操作也会是确定的一次开阀或关阀动作。
            target_mode = self.close_mode if baseline > 0 else self.open_mode
            direction = "decrease" if baseline > 0 else "increase"
            self.log(
                "action_started",
                action=action_number,
                baseline_percent=baseline,
                target_mode=target_mode,
                expected_direction=direction,
            )
            selected_status = self.select_mode(target_mode)
            self.validate_target_direction(selected_status, direction, target_mode)
            self.log(
                "target_verified",
                action=action_number,
                target_mode=target_mode,
                target_setpoint=selected_status.setpoint,
                room_temperature=selected_status.room_temperature,
            )
            status = self.wait_for_valve_change(baseline, direction, action_number)

        self.log("run_completed", moves_confirmed=moves)
        self.save_screenshot("run-completed")


def find_exact(nodes: Iterable[Node], text: str) -> Node | None:
    """返回文字完全匹配且最靠上的 UI 节点。"""
    matches = [node for node in nodes if node.text == text]
    return min(matches, key=lambda node: (node.bounds[1], node.bounds[0])) if matches else None


def menu_target_node(nodes: Iterable[Node], target_mode: str) -> Node | None:
    """定位模式选择弹层中的预设项，而不是页面主体上的同名文字。"""
    matches = [
        node
        for node in nodes
        if node.text != target_mode and target_mode in node.text and node.bounds[1] > 500
    ]
    # 底部弹层中的条目总是低于顶部模式胶囊和中部模式选择器。
    return max(matches, key=lambda node: node.bounds[1]) if matches else None


def read_large_setpoint(nodes: Iterable[Node]) -> float | None:
    """从 UI 层拆开的数字节点中还原中部的大字号设温。"""
    parts = [
        node
        for node in nodes
        if NUMBER_PART_RE.fullmatch(node.text) and node.height >= 80
    ]
    groups: list[list[Node]] = []
    # UIAutomator 会把 25.0 拆成 '25'、'.'、'0' 三个节点；先按同一视觉行
    # 分组，再按从左到右的顺序拼接，避免依赖屏幕截图 OCR。
    for part in sorted(parts, key=lambda node: (node.center[1], node.bounds[0])):
        for group in groups:
            if abs(group[0].center[1] - part.center[1]) <= 30:
                group.append(part)
                break
        else:
            groups.append([part])

    values: list[float] = []
    for group in groups:
        text = "".join(node.text for node in sorted(group, key=lambda node: node.bounds[0]))
        try:
            value = float(text)
        except ValueError:
            continue
        if 0.0 <= value <= 50.0:
            values.append(value)
    return values[0] if values else None


def build_parser() -> argparse.ArgumentParser:
    """定义命令行参数和偏安全的默认值。"""
    parser = argparse.ArgumentParser(
        description="在 PID 模式下循环切换涂鸦 TRV，并只统计已确认的开度变化。"
    )
    parser.add_argument("--device", default="705z#1", help="智能生活设备名，默认：705z#1")
    parser.add_argument("--moves", type=int, required=True, help="需要完成并确认的阀门动作次数")
    parser.add_argument("--timeout", type=float, default=15.0, help="每个 UI 条件最多等待秒数")
    parser.add_argument("--poll", type=float, default=1.0, help="读取 UI 的轮询间隔秒数")
    parser.add_argument(
        "--close-mode",
        default="节能",
        help="用于关阀的预设，其设温必须低于室温，默认：节能",
    )
    parser.add_argument(
        "--open-mode",
        default="舒适",
        help="用于开阀的预设，其设温必须高于室温，默认：舒适",
    )
    parser.add_argument("--serial", help="连接多台 Android 时指定 ADB 序列号")
    parser.add_argument("--output-dir", default="trv-runs", help="运行日志和截图的输出目录")
    return parser


def main() -> int:
    """校验参数、创建证据目录，并安全地启动整个循环。"""
    args = build_parser().parse_args()
    if args.moves < 1:
        print("--moves must be at least 1", file=sys.stderr)
        return 2
    if args.timeout <= 0 or args.poll <= 0:
        print("--timeout and --poll must both be positive", file=sys.stderr)
        return 2

    run_dir = Path(args.output_dir) / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    cycle = TrvCycle(
        Adb(args.serial),
        args.device,
        args.timeout,
        args.poll,
        run_dir,
        args.close_mode,
        args.open_mode,
    )
    try:
        cycle.run(args.moves)
    except TrvError as exc:
        cycle.log("run_stopped", error=str(exc))
        try:
            screenshot = cycle.save_screenshot("run-stopped")
            print(f"Stopped safely. Evidence: {screenshot}", file=sys.stderr)
        except TrvError:
            pass
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
