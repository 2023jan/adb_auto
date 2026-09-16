#!/usr/bin/env python3
"""在 PID 模式下循环切换涂鸦 TRV，并只统计已确认的阀门动作。

所有运行参数从本脚本同目录的 config.yaml 读取。手机需保持解锁并已登录
智能生活；只有设备上报的阀门开度按预期方向变化，才会计入一次动作。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Mapping

import yaml


PACKAGE = "com.tuya.smartlifeiot"
LAUNCH_ACTIVITY = "com.smart.ThingSplashActivity"
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.yaml"
PERCENT_RE = re.compile(r"^(\d{1,3})%$")
CELSIUS_RE = re.compile(r"^(\d+(?:\.\d+)?)℃$")
NUMBER_PART_RE = re.compile(r"^\d+$|^\.$")


class TrvError(RuntimeError):
    """出现不适合继续控制设备的状态时抛出的安全异常。"""


@dataclass(frozen=True)
class Node:
    """Android UI 节点，包含文字、屏幕范围与父子关系。"""

    text: str
    bounds: tuple[int, int, int, int]
    class_name: str
    children: tuple["Node", ...]

    @property
    def center(self) -> tuple[int, int]:
        """返回元素中心，用于点击已定位的 UI 元素。"""
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)

    @property
    def width(self) -> int:
        """返回节点宽度。"""
        return self.bounds[2] - self.bounds[0]

    @property
    def height(self) -> int:
        """返回节点高度。"""
        return self.bounds[3] - self.bounds[1]

    def descendants(self) -> Iterator["Node"]:
        """深度优先遍历当前节点和所有子节点。"""
        yield self
        for child in self.children:
            yield from child.descendants()

    def identity(self) -> tuple[str, tuple[int, int, int, int], str]:
        """返回用于比较两个 UI 快照中同一元素的标识。"""
        return (self.text, self.bounds, self.class_name)


@dataclass(frozen=True)
class UiSnapshot:
    """一次 UIAutomator XML 读取结果。"""

    roots: tuple[Node, ...]
    xml: str

    def nodes(self) -> tuple[Node, ...]:
        """返回快照中所有节点。"""
        return tuple(node for root in self.roots for node in root.descendants())


@dataclass(frozen=True)
class HeaderStatus:
    """顶部状态栏中的当前模式、阀门开度和电量节点。"""

    mode_node: Node
    valve_node: Node
    battery_node: Node


@dataclass(frozen=True)
class Status:
    """从 UI 读取到的 TRV 状态。"""

    mode: str
    valve_percent: int
    battery_percent: int
    room_temperature: float
    setpoint: float | None


@dataclass(frozen=True)
class RunConfig:
    """经 YAML 校验后的全部运行参数。"""

    device_name: str
    serial: str | None
    moves: int
    timeout: float
    poll_interval: float
    close_mode: str
    open_mode: str
    output_dir: Path


def parse_bounds(value: str) -> tuple[int, int, int, int] | None:
    """将 Android 的 bounds 属性转换为整数坐标。"""
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value)
    if not match:
        return None
    left, top, right, bottom = (int(item) for item in match.groups())
    return left, top, right, bottom


def parse_snapshot(xml: str) -> UiSnapshot:
    """使用 XML 解析器构建保留层级关系的 UI 快照。"""
    try:
        hierarchy = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise TrvError(f"无法解析 Android UI XML: {exc}") from exc

    def build(element: ET.Element) -> Node | None:
        bounds = parse_bounds(element.attrib.get("bounds", ""))
        if bounds is None:
            return None
        children = tuple(
            node for child in element.findall("node") if (node := build(child)) is not None
        )
        return Node(
            text=element.attrib.get("text", "").strip(),
            bounds=bounds,
            class_name=element.attrib.get("class", ""),
            children=children,
        )

    roots = tuple(node for item in hierarchy.findall("node") if (node := build(item)) is not None)
    return UiSnapshot(roots=roots, xml=xml)


def require_mapping(value: object, name: str) -> Mapping[str, object]:
    """校验 YAML 段为键值对象。"""
    if not isinstance(value, dict):
        raise TrvError(f"config.yaml 中的 {name} 必须是键值对象。")
    return value


def require_string(section: Mapping[str, object], key: str, section_name: str) -> str:
    """读取非空字符串配置项。"""
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TrvError(f"config.yaml 中的 {section_name}.{key} 必须是非空字符串。")
    return value.strip()


def require_positive_number(section: Mapping[str, object], key: str, section_name: str) -> float:
    """读取大于零的数值配置项。"""
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise TrvError(f"config.yaml 中的 {section_name}.{key} 必须大于 0。")
    return float(value)


def require_positive_int(section: Mapping[str, object], key: str, section_name: str) -> int:
    """读取大于零的整数配置项。"""
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TrvError(f"config.yaml 中的 {section_name}.{key} 必须是大于 0 的整数。")
    return value


def load_config(path: Path) -> RunConfig:
    """安全读取并完整校验 config.yaml，在 ADB 操作前阻止错误配置。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TrvError(f"未找到配置文件：{path}") from exc
    except yaml.YAMLError as exc:
        raise TrvError(f"config.yaml 不是有效 YAML: {exc}") from exc

    config = require_mapping(raw, "根节点")
    device = require_mapping(config.get("device"), "device")
    cycle = require_mapping(config.get("cycle"), "cycle")
    output = require_mapping(config.get("output"), "output")
    serial = device.get("serial")
    if serial is not None and (not isinstance(serial, str) or not serial.strip()):
        raise TrvError("config.yaml 中的 device.serial 必须是字符串或 null。")
    close_mode = require_string(cycle, "close_mode", "cycle")
    open_mode = require_string(cycle, "open_mode", "cycle")
    if close_mode == open_mode:
        raise TrvError("cycle.close_mode 和 cycle.open_mode 必须不同。")

    output_dir = Path(require_string(output, "runs_dir", "output")).expanduser()
    if not output_dir.is_absolute():
        output_dir = path.parent / output_dir
    return RunConfig(
        device_name=require_string(device, "name", "device"),
        serial=serial.strip() if isinstance(serial, str) else None,
        moves=require_positive_int(cycle, "moves", "cycle"),
        timeout=require_positive_number(cycle, "timeout_seconds", "cycle"),
        poll_interval=require_positive_number(cycle, "poll_interval_seconds", "cycle"),
        close_mode=close_mode,
        open_mode=open_mode,
        output_dir=output_dir,
    )


def find_exact(snapshot: UiSnapshot, text: str) -> Node | None:
    """返回文字完全匹配且最靠上的 UI 节点。"""
    matches = [node for node in snapshot.nodes() if node.text == text]
    return min(matches, key=lambda node: (node.bounds[1], node.bounds[0])) if matches else None


def read_header(snapshot: UiSnapshot) -> HeaderStatus:
    """结构化解析顶部状态栏，允许当前模式为自定义或任意其他模式。"""
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
            # 模式在状态栏最左侧；第一个百分比为阀门开度，第二个为电量。
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
        raise TrvError("顶部仅识别到一个百分比，疑似处于 on/off 模式而不是 PID 模式。")
    if all_percentages:
        raise TrvError("识别到百分比，但未找到 PID 的模式、开度、电量状态栏。")
    raise TrvError("未识别到 PID 顶部状态栏；页面可能尚未加载完成。")


def read_large_setpoint(snapshot: UiSnapshot) -> float | None:
    """还原中部大字号设温，使用相对文字面积而非固定像素阈值。"""
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
    """定位打开菜单后新增的目标项，避免点击页面上已有的同名模式。"""
    matches = [node for node in snapshot.nodes() if target_mode in node.text]
    newly_visible = [node for node in matches if node.identity() not in before_menu]
    candidates = newly_visible or [node for node in matches if node.text != target_mode]
    if not candidates:
        return None
    return max(candidates, key=lambda node: (len(node.text), node.bounds[1]))


class Adb:
    """封装 ADB 的 UI 读取、截图与点击操作。"""

    def __init__(self, serial: str | None) -> None:
        """构造 ADB 命令前缀。"""
        self.prefix = ["adb"] + (["-s", serial] if serial else [])
        self.last_ui_xml: str | None = None

    def run(self, *args: str, binary: bool = False) -> str | bytes:
        """运行 ADB 子命令并将失败转为安全异常。"""
        command = self.prefix + list(args)
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise TrvError(f"ADB 命令失败：{' '.join(command)}\n{stderr}")
        return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")

    def tap(self, x: int, y: int) -> None:
        """点击一个由 UI bounds 实时定位的坐标。"""
        self.run("shell", "input", "tap", str(x), str(y))

    def start_app(self) -> None:
        """将智能生活启动到前台。"""
        self.run("shell", "am", "start", "-n", f"{PACKAGE}/{LAUNCH_ACTIVITY}")

    def dump_ui(self) -> UiSnapshot:
        """读取 UIAutomator XML，并移除 Honor 设备可能附带的尾部状态文字。"""
        output = self.run("exec-out", "uiautomator", "dump", "--compressed", "/dev/tty")
        start, end = output.find("<hierarchy"), output.rfind("</hierarchy>")
        if start < 0 or end < 0:
            raise TrvError("未读取到完整 Android UI XML。")
        self.last_ui_xml = output[start : end + len("</hierarchy>")]
        return parse_snapshot(self.last_ui_xml)

    def screenshot(self, destination: Path) -> None:
        """保存当前手机截图。"""
        destination.write_bytes(self.run("exec-out", "screencap", "-p", binary=True))

    def save_last_ui_xml(self, destination: Path) -> None:
        """保存最近一次成功读取的 UI XML。"""
        if self.last_ui_xml is None:
            raise TrvError("尚未读取到可保存的 UI XML。")
        destination.write_text(self.last_ui_xml, encoding="utf-8")


class TrvCycle:
    """执行页面恢复、模式选择、PID 校验、阀门计数与证据保存。"""

    def __init__(self, adb: Adb, config: RunConfig, run_dir: Path) -> None:
        """保存本次执行所需的已校验配置。"""
        self.adb, self.config, self.run_dir = adb, config, run_dir
        self.log_path = run_dir / "events.jsonl"

    def log(self, event: str, **fields: object) -> None:
        """写入 JSONL 运行日志并打印到终端。"""
        record = {"time": datetime.now().isoformat(timespec="seconds"), "event": event, **fields}
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def evidence(self, label: str, xml: bool = False) -> Path:
        """保存截图或最近 UI XML 作为测试证据。"""
        suffix = "xml" if xml else "png"
        path = self.run_dir / f"{datetime.now():%H%M%S}-{safe_label(label)}.{suffix}"
        if xml:
            self.adb.save_last_ui_xml(path)
        else:
            self.adb.screenshot(path)
        return path

    def is_target_page(self, snapshot: UiSnapshot) -> bool:
        """判断是否到达配置指定设备的 TRV 页面。"""
        texts = {node.text for node in snapshot.nodes()}
        return self.config.device_name in texts and "模式" in texts

    def wait_until(self, predicate: Callable[[UiSnapshot], bool], description: str) -> UiSnapshot:
        """在配置超时内轮询 UI，直到条件成立。"""
        deadline, last_error = time.monotonic() + self.config.timeout, None
        while time.monotonic() < deadline:
            try:
                snapshot = self.adb.dump_ui()
                if predicate(snapshot):
                    return snapshot
            except TrvError as exc:
                last_error = exc
            time.sleep(self.config.poll_interval)
        raise TrvError(f"等待 {description} 超时。最后错误：{last_error}")

    def read_status(self, snapshot: UiSnapshot) -> Status:
        """读取当前任意模式、阀门开度、电量、室温和设温。"""
        header = read_header(snapshot)
        valve = int(PERCENT_RE.fullmatch(header.valve_node.text).group(1))  # type: ignore[union-attr]
        battery = int(PERCENT_RE.fullmatch(header.battery_node.text).group(1))  # type: ignore[union-attr]
        temperatures = [
            (node, float(match.group(1)))
            for node in snapshot.nodes()
            if (match := CELSIUS_RE.fullmatch(node.text))
        ]
        if not temperatures:
            raise TrvError("未读取到带 ℃ 标识的室温。")
        return Status(
            mode=header.mode_node.text,
            valve_percent=valve,
            battery_percent=battery,
            room_temperature=min(temperatures, key=lambda item: item[0].bounds[1])[1],
            setpoint=read_large_setpoint(snapshot),
        )

    def wait_for_status(self, description: str) -> Status:
        """等待顶部 PID 状态栏和大字号设温完整加载。"""
        deadline, last_error = time.monotonic() + self.config.timeout, None
        while time.monotonic() < deadline:
            try:
                status = self.read_status(self.adb.dump_ui())
                if status.setpoint is not None:
                    return status
                last_error = TrvError("未识别到中部大字号设温。")
            except TrvError as exc:
                last_error = exc
            time.sleep(self.config.poll_interval)
        raise TrvError(f"等待 {description} 超时。最后状态：{last_error}")

    def ensure_target_page(self) -> None:
        """复用目标页，或启动智能生活后从首页打开目标设备。"""
        snapshot = self.adb.dump_ui()
        if self.is_target_page(snapshot):
            self.log("using_current_device_page", device=self.config.device_name)
            return
        self.log("opening_smart_life", device=self.config.device_name)
        self.adb.start_app()
        deadline = time.monotonic() + self.config.timeout
        while time.monotonic() < deadline:
            snapshot = self.adb.dump_ui()
            if self.is_target_page(snapshot):
                return
            device_node = find_exact(snapshot, self.config.device_name)
            if device_node:
                self.log("opening_device", device=self.config.device_name, bounds=device_node.bounds)
                self.adb.tap(*device_node.center)
                self.wait_until(self.is_target_page, f"设备 {self.config.device_name} 的 TRV 页面")
                return
            time.sleep(self.config.poll_interval)
        raise TrvError(f"打开智能生活后未找到设备 {self.config.device_name}。")

    def select_mode(self, target_mode: str) -> Status:
        """点击顶部实际模式胶囊，从弹层选择目标预设并等待生效。"""
        snapshot = self.adb.dump_ui()
        current = self.read_status(snapshot)
        if current.mode == target_mode and current.setpoint is not None:
            return current
        header = read_header(snapshot)
        before_menu = {node.identity() for node in snapshot.nodes()}
        self.log("opening_mode_menu", current_mode=current.mode, target_mode=target_mode)
        self.adb.tap(*header.mode_node.center)
        menu = self.wait_until(
            lambda item: menu_target_node(item, target_mode, before_menu) is not None,
            f"模式菜单中的 {target_mode}",
        )
        target = menu_target_node(menu, target_mode, before_menu)
        assert target is not None
        self.log("selecting_mode", target_mode=target_mode, bounds=target.bounds)
        self.adb.tap(*target.center)

        def applied(item: UiSnapshot) -> bool:
            try:
                status = self.read_status(item)
            except TrvError:
                return False
            return status.mode == target_mode and status.setpoint is not None

        return self.read_status(self.wait_until(applied, f"{target_mode} 模式生效"))

    def validate_direction(self, status: Status, direction: str, mode: str) -> None:
        """确认目标设温与室温关系足以驱动 PID 开度向目标方向变化。"""
        assert status.setpoint is not None
        if direction == "decrease" and status.setpoint >= status.room_temperature:
            raise TrvError(f"{mode} 设温 {status.setpoint:.1f}℃ 不低于室温 {status.room_temperature:.1f}℃，无法确认关阀。")
        if direction == "increase" and status.setpoint <= status.room_temperature:
            raise TrvError(f"{mode} 设温 {status.setpoint:.1f}℃ 不高于室温 {status.room_temperature:.1f}℃，无法确认开阀。")

    def wait_for_change(self, baseline: int, direction: str, action: int) -> Status:
        """等待设备上报开度相对基线按预期方向变化。"""
        deadline, latest = time.monotonic() + self.config.timeout, None
        while time.monotonic() < deadline:
            try:
                latest = self.read_status(self.adb.dump_ui())
                changed = (direction == "increase" and latest.valve_percent > baseline) or (
                    direction == "decrease" and latest.valve_percent < baseline
                )
                if changed:
                    self.log(
                        "valve_motion_confirmed",
                        action=action,
                        direction=direction,
                        before_percent=baseline,
                        after_percent=latest.valve_percent,
                        mode=latest.mode,
                        room_temperature=latest.room_temperature,
                    )
                    return latest
            except TrvError as exc:
                self.log("state_read_retry", action=action, error=str(exc))
            time.sleep(self.config.poll_interval)
        screenshot, xml = self.evidence(f"action-{action}-no-valve-change"), self.evidence(f"action-{action}-no-valve-change", xml=True)
        raise TrvError(f"第 {action} 次动作未在 {self.config.timeout:.0f} 秒内从 {baseline}% 向 {direction} 变化；截图：{screenshot}；XML：{xml}")

    def run(self) -> None:
        """执行配置指定数量的、由开度变化确认的阀门动作。"""
        self.ensure_target_page()
        status = self.wait_for_status("PID 状态栏加载")
        self.log(
            "run_started",
            moves_requested=self.config.moves,
            initial_mode=status.mode,
            initial_valve_percent=status.valve_percent,
            room_temperature=status.room_temperature,
            setpoint=status.setpoint,
            timeout_seconds=self.config.timeout,
        )
        for action in range(1, self.config.moves + 1):
            baseline = status.valve_percent
            mode = self.config.close_mode if baseline > 0 else self.config.open_mode
            direction = "decrease" if baseline > 0 else "increase"
            self.log("action_started", action=action, baseline_percent=baseline, target_mode=mode, expected_direction=direction)
            selected = self.select_mode(mode)
            self.validate_direction(selected, direction, mode)
            self.log("target_verified", action=action, target_mode=mode, target_setpoint=selected.setpoint, room_temperature=selected.room_temperature)
            status = self.wait_for_change(baseline, direction, action)
        self.log("run_completed", moves_confirmed=self.config.moves)
        self.evidence("run-completed")
        self.evidence("run-completed", xml=True)


def safe_label(label: str) -> str:
    """将标签转换为安全的 ASCII 文件名。"""
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", label)


def main() -> int:
    """读取固定 YAML 配置并在失败时保存截图与 UI XML。"""
    if len(sys.argv) != 1:
        print("不支持命令行参数。请修改 scripts/config.yaml 后直接执行脚本。", file=sys.stderr)
        return 2
    try:
        config = load_config(CONFIG_PATH)
    except TrvError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    run_dir = config.output_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    cycle = TrvCycle(Adb(config.serial), config, run_dir)
    try:
        cycle.run()
    except TrvError as exc:
        cycle.log("run_stopped", error=str(exc))
        evidence = []
        for xml in (False, True):
            try:
                evidence.append(str(cycle.evidence("run-stopped", xml=xml)))
            except TrvError:
                pass
        if evidence:
            print(f"已安全停止。证据：{'；'.join(evidence)}", file=sys.stderr)
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
