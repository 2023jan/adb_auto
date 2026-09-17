"""通过 App 切换 PID 模式并按真实阀门开度变化计数。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Mapping

from core.adb import Adb
from core.config import (
    AutomationConfig,
    require_positive_int,
    require_positive_number,
    require_string,
)
from core.evidence import RunArtifacts
from core.errors import AutomationError
from pages.smart_life_home import SmartLifeHome
from pages.trv_panel import TrvPanel, TrvStatus


@dataclass(frozen=True)
class TrvValveCycleConfig:
    """TRV 阀门循环场景独有的业务参数。"""

    moves: int
    timeout: float
    poll_interval: float
    close_mode: str
    open_mode: str

    @classmethod
    def from_settings(cls, settings: Mapping[str, object]) -> "TrvValveCycleConfig":
        """校验 ``scenarios.trv_valve_cycle`` 的全部参数。"""
        section = "scenarios.trv_valve_cycle"
        close_mode = require_string(settings, "close_mode", section)
        open_mode = require_string(settings, "open_mode", section)
        if close_mode == open_mode:
            raise AutomationError("TRV 的 close_mode 和 open_mode 必须不同。")
        return cls(
            moves=require_positive_int(settings, "moves", section),
            timeout=require_positive_number(settings, "timeout_seconds", section),
            poll_interval=require_positive_number(settings, "poll_interval_seconds", section),
            close_mode=close_mode,
            open_mode=open_mode,
        )


class TrvValveCycle:
    """TRV 阀门循环的完整业务流程。"""

    def __init__(self, config: AutomationConfig, adb: Adb, artifacts: RunArtifacts) -> None:
        """构造本场景所需的页面对象和已校验配置引用。"""
        self.config = config
        self.cycle = TrvValveCycleConfig.from_settings(config.scenario_settings)
        self.artifacts = artifacts
        self.panel = TrvPanel(adb, config.device.name, self.cycle.timeout, self.cycle.poll_interval)
        self.home = SmartLifeHome(
            adb,
            config.app.package,
            config.app.launch_activity,
            self.cycle.timeout,
            self.cycle.poll_interval,
            config.app.device_search_max_swipes,
        )

    def run(self) -> None:
        """完成配置次数的阀门动作，并只统计已确认开度变化。"""
        already_open = self.home.ensure_device_page(self.config.device.name, self.panel.is_target_page)
        self.artifacts.log(
            "using_current_device_page" if already_open else "opened_device_page",
            device=self.config.device.name,
        )
        status = self.panel.wait_for_status("PID 状态栏加载")
        self.artifacts.log(
            "run_started",
            moves_requested=self.cycle.moves,
            initial_mode=status.mode,
            initial_valve_percent=status.valve_percent,
            room_temperature=status.room_temperature,
            setpoint=status.setpoint,
            timeout_seconds=self.cycle.timeout,
        )

        for action in range(1, self.cycle.moves + 1):
            baseline = status.valve_percent
            mode = self.cycle.close_mode if baseline > 0 else self.cycle.open_mode
            direction = "decrease" if baseline > 0 else "increase"
            self.artifacts.log(
                "action_started",
                action=action,
                baseline_percent=baseline,
                target_mode=mode,
                expected_direction=direction,
            )
            selected = self.panel.select_mode(mode)
            self._validate_direction(selected, direction, mode)
            self.artifacts.log(
                "target_verified",
                action=action,
                target_mode=mode,
                target_setpoint=selected.setpoint,
                room_temperature=selected.room_temperature,
            )
            status = self._wait_for_valve_change(baseline, direction, action)

        self.artifacts.log("run_completed", moves_confirmed=self.cycle.moves)
        self.artifacts.screenshot("run-completed")
        self.artifacts.ui_xml("run-completed")

    def _validate_direction(self, status: TrvStatus, direction: str, mode: str) -> None:
        """确认目标模式设温与室温的关系确实能驱动 PID 阀门变化。"""
        assert status.setpoint is not None
        if direction == "decrease" and status.setpoint >= status.room_temperature:
            raise AutomationError(
                f"{mode} 设温 {status.setpoint:.1f}℃ 不低于室温 {status.room_temperature:.1f}℃，无法确认关阀。"
            )
        if direction == "increase" and status.setpoint <= status.room_temperature:
            raise AutomationError(
                f"{mode} 设温 {status.setpoint:.1f}℃ 不高于室温 {status.room_temperature:.1f}℃，无法确认开阀。"
            )

    def _wait_for_valve_change(self, baseline: int, direction: str, action: int) -> TrvStatus:
        """等待设备上报相对基线向指定方向变化的阀门开度。"""
        deadline = time.monotonic() + self.cycle.timeout
        last_error: AutomationError | None = None
        while time.monotonic() < deadline:
            try:
                latest = self.panel.read_status(self.panel.adb.dump_ui())
                changed = (direction == "increase" and latest.valve_percent > baseline) or (
                    direction == "decrease" and latest.valve_percent < baseline
                )
                if changed:
                    self.artifacts.log(
                        "valve_motion_confirmed",
                        action=action,
                        direction=direction,
                        before_percent=baseline,
                        after_percent=latest.valve_percent,
                        mode=latest.mode,
                        room_temperature=latest.room_temperature,
                    )
                    return latest
            except AutomationError as exc:
                last_error = exc
                self.artifacts.log("state_read_retry", action=action, error=str(exc))
            time.sleep(self.cycle.poll_interval)

        evidence = self.artifacts.failure_evidence(f"action-{action}-no-valve-change")
        evidence_text = "；".join(str(path) for path in evidence) or "未能保存证据"
        raise AutomationError(
            f"第 {action} 次动作未在 {self.cycle.timeout:.0f} 秒内从 {baseline}% 向 {direction} 变化；证据：{evidence_text}；最后错误：{last_error}"
        )
