"""唯一启动入口：加载配置、创建运行目录并调度所选业务场景。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from core.adb import Adb
from core.config import load_config
from core.evidence import RunArtifacts
from core.errors import AutomationError
from workflows import run_selected_scenario

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = PROJECT_DIR / "config"


def main() -> int:
    """执行 config/run.yaml 指定场景；命令行不接收运行参数。"""
    if len(sys.argv) != 1:
        print("不支持命令行参数。请修改 config/ 中的 YAML 后直接执行。", file=sys.stderr)
        return 2
    try:
        config = load_config(CONFIG_DIR)
    except AutomationError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    run_dir = config.output_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    adb = Adb(config.device.serial)
    artifacts = RunArtifacts(adb, run_dir)
    artifacts.log("scenario_started", scenario=config.run.scenario)
    try:
        adb.require_unlocked()
        run_selected_scenario(config, adb, artifacts)
    except AutomationError as exc:
        artifacts.log("run_stopped", error=str(exc))
        evidence = artifacts.failure_evidence("run-stopped")
        if evidence:
            print(f"已安全停止。证据：{'；'.join(str(path) for path in evidence)}", file=sys.stderr)
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
