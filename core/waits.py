"""等待 App 异步页面状态的通用轮询工具。"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from .errors import AutomationError

T = TypeVar("T")


def wait_until(
    read: Callable[[], T],
    matches: Callable[[T], bool],
    timeout: float,
    poll_interval: float,
    description: str,
) -> T:
    """重复读取状态，直至谓词成立或超时；读取异常会在超时前重试。"""
    deadline = time.monotonic() + timeout
    last_error: AutomationError | None = None
    while time.monotonic() < deadline:
        try:
            value = read()
            if matches(value):
                return value
        except AutomationError as exc:
            last_error = exc
        time.sleep(poll_interval)
    suffix = f"最后错误：{last_error}" if last_error else "未达到预期状态。"
    raise AutomationError(f"等待 {description} 超时。{suffix}")
