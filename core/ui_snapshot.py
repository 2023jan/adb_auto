"""Android UIAutomator XML 的解析与通用节点查找。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterator

from .errors import AutomationError


@dataclass(frozen=True)
class Node:
    """Android UI 节点，包含文字、屏幕范围与父子关系。"""

    text: str
    bounds: tuple[int, int, int, int]
    class_name: str
    children: tuple["Node", ...]

    @property
    def center(self) -> tuple[int, int]:
        """返回节点中心坐标，可交给 ADB 点击。"""
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
        """深度优先遍历当前节点及所有后代。"""
        yield self
        for child in self.children:
            yield from child.descendants()

    def identity(self) -> tuple[str, tuple[int, int, int, int], str]:
        """返回用于比较不同快照中同一节点的稳定标识。"""
        return (self.text, self.bounds, self.class_name)


@dataclass(frozen=True)
class UiSnapshot:
    """一次 UIAutomator XML 读取结果。"""

    roots: tuple[Node, ...]
    xml: str

    def nodes(self) -> tuple[Node, ...]:
        """返回快照中的所有节点。"""
        return tuple(node for root in self.roots for node in root.descendants())


def parse_bounds(value: str) -> tuple[int, int, int, int] | None:
    """将 Android ``bounds`` 属性转换为整数坐标。"""
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value)
    if not match:
        return None
    left, top, right, bottom = (int(item) for item in match.groups())
    return left, top, right, bottom


def parse_snapshot(xml: str) -> UiSnapshot:
    """用 XML 解析器构建并保留 UI 元素层级。"""
    try:
        hierarchy = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise AutomationError(f"无法解析 Android UI XML: {exc}") from exc

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


def find_exact(snapshot: UiSnapshot, text: str) -> Node | None:
    """返回文字完全匹配且位置最靠上的节点。"""
    matches = [node for node in snapshot.nodes() if node.text == text]
    return min(matches, key=lambda node: (node.bounds[1], node.bounds[0])) if matches else None
