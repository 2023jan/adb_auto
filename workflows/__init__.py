"""按业务场景组织的测试流程。"""

from __future__ import annotations

from core.adb import Adb
from core.config import AutomationConfig
from core.evidence import RunArtifacts
from core.errors import AutomationError
from .trv_valve_cycle import TrvValveCycle


def run_selected_scenario(config: AutomationConfig, adb: Adb, artifacts: RunArtifacts) -> None:
    """根据 ``run.scenario`` 创建并执行对应 workflow。"""
    if config.run.scenario == "trv_valve_cycle":
        TrvValveCycle(config, adb, artifacts).run()
        return
    raise AutomationError(f"不支持的场景：{config.run.scenario}")
