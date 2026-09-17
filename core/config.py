"""组合读取配置目录中的实验室、设备实例和业务场景配置。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from .errors import AutomationError


@dataclass(frozen=True)
class RunConfig:
    """本次启动选择的业务场景和设备实例配置名。"""

    scenario: str
    device_profile: str


@dataclass(frozen=True)
class AppConfig:
    """受测 Android App 的启动信息。"""

    package: str
    launch_activity: str
    device_search_max_swipes: int


@dataclass(frozen=True)
class DeviceConfig:
    """当前测试设备在 App 中的显示名称和 ADB 连接信息。"""

    name: str
    serial: str | None


@dataclass(frozen=True)
class AutomationConfig:
    """由多个 YAML 文件组合出的已校验运行配置。"""

    run: RunConfig
    app: AppConfig
    device: DeviceConfig
    output_dir: Path
    scenario_settings: Mapping[str, object]


def require_mapping(value: object, name: str) -> Mapping[str, object]:
    """校验 YAML 节点是键值对象。"""
    if not isinstance(value, dict):
        raise AutomationError(f"配置中的 {name} 必须是键值对象。")
    return value


def require_string(section: Mapping[str, object], key: str, section_name: str) -> str:
    """读取指定节点中的非空字符串。"""
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AutomationError(f"配置中的 {section_name}.{key} 必须是非空字符串。")
    return value.strip()


def require_positive_number(section: Mapping[str, object], key: str, section_name: str) -> float:
    """读取指定节点中的正数。"""
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise AutomationError(f"配置中的 {section_name}.{key} 必须大于 0。")
    return float(value)


def require_positive_int(section: Mapping[str, object], key: str, section_name: str) -> int:
    """读取指定节点中的正整数。"""
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AutomationError(f"配置中的 {section_name}.{key} 必须是大于 0 的整数。")
    return value


def load_config(config_dir: Path) -> AutomationConfig:
    """读取 ``config/`` 目录，并按 ``run.yaml`` 组合当前设备和场景。"""
    config_dir = config_dir.resolve()
    run = _load_yaml(config_dir / "run.yaml")
    lab_path = config_dir / "lab.yaml"
    lab = _load_yaml(lab_path)
    run_section = require_mapping(run.get("run"), "run.yaml: run")
    scenario = _profile_name(require_string(run_section, "scenario", "run.yaml: run"), "场景")
    device_profile = _profile_name(require_string(run_section, "device", "run.yaml: run"), "设备")

    device_file = config_dir / "devices" / f"{device_profile}.yaml"
    device = _load_yaml(device_file)
    scenario_file = config_dir / "scenarios" / f"{scenario}.yaml"
    scenario_settings = _load_yaml(scenario_file)

    app_section = require_mapping(lab.get("app"), "lab.yaml: app")
    adb_section = require_mapping(lab.get("adb"), "lab.yaml: adb")
    output_section = require_mapping(lab.get("output"), "lab.yaml: output")
    device_section = require_mapping(device.get("device"), f"{device_file.name}: device")
    serial = adb_section.get("serial")
    if serial is not None and (not isinstance(serial, str) or not serial.strip()):
        raise AutomationError("配置中的 lab.yaml: adb.serial 必须是字符串或 null。")

    runs_dir = Path(require_string(output_section, "runs_dir", "lab.yaml: output")).expanduser()
    output_dir = runs_dir if runs_dir.is_absolute() else (lab_path.parent / runs_dir).resolve()
    return AutomationConfig(
        run=RunConfig(scenario=scenario, device_profile=device_profile),
        app=AppConfig(
            package=require_string(app_section, "package", "lab.yaml: app"),
            launch_activity=require_string(app_section, "launch_activity", "lab.yaml: app"),
            device_search_max_swipes=require_positive_int(
                app_section,
                "device_search_max_swipes",
                "lab.yaml: app",
            ),
        ),
        device=DeviceConfig(
            name=require_string(device_section, "name", f"{device_file.name}: device"),
            serial=serial.strip() if isinstance(serial, str) else None,
        ),
        output_dir=output_dir,
        scenario_settings=scenario_settings,
    )


def _load_yaml(path: Path) -> Mapping[str, object]:
    """读取一个 YAML 文件，并将文件名包含在配置错误中。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AutomationError(f"未找到配置文件：{path}") from exc
    except yaml.YAMLError as exc:
        raise AutomationError(f"配置文件不是有效 YAML：{path}: {exc}") from exc
    return require_mapping(raw, str(path))


def _profile_name(value: str, label: str) -> str:
    """验证配置名只能是一个文件名，防止配置引用离开 ``config/`` 目录。"""
    candidate = Path(value)
    if candidate.name != value or value in {".", ".."}:
        raise AutomationError(f"{label}配置名必须是不含路径的文件名。")
    return value
