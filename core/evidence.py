"""统一保存运行事件、手机截图与 UI XML 证据。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .adb import Adb
from .errors import AutomationError


class RunArtifacts:
    """管理单次运行目录下的 JSONL 日志和故障证据。"""

    def __init__(self, adb: Adb, run_dir: Path) -> None:
        """绑定本次运行使用的手机和证据目录。"""
        self.adb = adb
        self.run_dir = run_dir
        self.log_path = run_dir / "events.jsonl"

    def log(self, event: str, **fields: object) -> None:
        """追加 JSONL 事件，同时在终端输出相同内容。"""
        record = {"time": datetime.now().isoformat(timespec="seconds"), "event": event, **fields}
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def screenshot(self, label: str) -> Path:
        """保存当前手机截图。"""
        path = self._path(label, "png")
        self.adb.screenshot(path)
        return path

    def ui_xml(self, label: str) -> Path:
        """保存最近一次读取的 UI XML。"""
        path = self._path(label, "xml")
        self.adb.save_last_ui_xml(path)
        return path

    def failure_evidence(self, label: str) -> list[Path]:
        """尽量保存截图和 XML；其中一项失败不会妨碍另一项保存。"""
        evidence: list[Path] = []
        for capture in (self.screenshot, self.ui_xml):
            try:
                evidence.append(capture(label))
            except AutomationError:
                pass
        return evidence

    def _path(self, label: str, suffix: str) -> Path:
        """生成带时间且仅含 ASCII 字符的证据文件路径。"""
        safe_label = re.sub(r"[^a-zA-Z0-9_.-]+", "-", label)
        return self.run_dir / f"{datetime.now():%H%M%S}-{safe_label}.{suffix}"
