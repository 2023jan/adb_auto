"""TRV 自动化的离线 UI、配置和预检测试，不连接 Android 设备。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.adb import Adb
from core.config import load_config
from core.errors import AutomationError
from core.ui_snapshot import parse_snapshot
from pages.smart_life_home import SmartLifeHome
from pages.trv_panel import read_header, read_large_setpoint


PID_CUSTOM_XML = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy>
  <node text='' class='android.widget.FrameLayout' bounds='[0,0][1264,2736]'>
    <node text='' class='android.widget.HorizontalScrollView' bounds='[0,305][1264,465]'>
      <node text='自定义' class='android.widget.TextView' bounds='[178,374][256,427]' />
      <node text='12%' class='android.widget.TextView' bounds='[515,374][597,427]' />
      <node text='29%' class='android.widget.TextView' bounds='[800,374][882,427]' />
    </node>
    <node text='室温' class='android.widget.TextView' bounds='[468,584][588,656]' />
    <node text='18.6℃' class='android.widget.TextView' bounds='[600,584][785,656]' />
    <node text='18' class='android.widget.TextView' bounds='[428,689][672,904]' />
    <node text='.' class='android.widget.TextView' bounds='[672,689][714,904]' />
    <node text='5' class='android.widget.TextView' bounds='[714,689][836,904]' />
  </node>
</hierarchy>
"""


class HeaderParsingTests(unittest.TestCase):
    """验证顶部状态栏不依赖节能或舒适作为当前模式。"""

    def test_custom_mode_exposes_valve_before_battery(self) -> None:
        """自定义模式下，第一枚百分比必须识别为阀门开度。"""
        snapshot = parse_snapshot(PID_CUSTOM_XML)
        header = read_header(snapshot)
        self.assertEqual(header.mode_node.text, "自定义")
        self.assertEqual(header.valve_node.text, "12%")
        self.assertEqual(header.battery_node.text, "29%")
        self.assertEqual(read_large_setpoint(snapshot), 18.5)

    def test_one_percentage_is_rejected_as_non_pid(self) -> None:
        """仅有一枚百分比时应报告疑似 on/off，而非误认为阀门开度。"""
        snapshot = parse_snapshot(
            PID_CUSTOM_XML.replace(
                "<node text='12%' class='android.widget.TextView' bounds='[515,374][597,427]' />",
                "",
            )
        )
        with self.assertRaisesRegex(AutomationError, "on/off"):
            read_header(snapshot)


class ConfigTests(unittest.TestCase):
    """验证实验室、设备、场景配置可独立维护并组合运行。"""

    def test_loads_all_runtime_values_from_config_directory(self) -> None:
        """设备名、场景参数和 ADB 序列号均不能依赖脚本硬编码。"""
        run_content = """run:
  scenario: trv_valve_cycle
  device: other_trv
"""
        lab_content = """app:
  package: com.tuya.smartlifeiot
  launch_activity: com.smart.ThingSplashActivity
  device_search_max_swipes: 4
adb:
  serial: serial-1
output:
  runs_dir: ../evidence
"""
        device_content = """device:
  name: other-trv
"""
        scenario_content = """moves: 4
timeout_seconds: 15
poll_interval_seconds: 1
close_mode: 防冻
open_mode: 舒适
"""
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "config"
            (config_dir / "devices").mkdir(parents=True)
            (config_dir / "scenarios").mkdir()
            (config_dir / "run.yaml").write_text(run_content, encoding="utf-8")
            (config_dir / "lab.yaml").write_text(lab_content, encoding="utf-8")
            (config_dir / "devices" / "other_trv.yaml").write_text(device_content, encoding="utf-8")
            (config_dir / "scenarios" / "trv_valve_cycle.yaml").write_text(scenario_content, encoding="utf-8")

            config = load_config(config_dir)

        self.assertEqual(config.run.scenario, "trv_valve_cycle")
        self.assertEqual(config.run.device_profile, "other_trv")
        self.assertEqual(config.device.name, "other-trv")
        self.assertEqual(config.device.serial, "serial-1")
        self.assertEqual(config.scenario_settings["moves"], 4)
        self.assertEqual(config.scenario_settings["close_mode"], "防冻")
        self.assertEqual(config.output_dir, (Path(directory) / "evidence").resolve())


class DevicePreflightTests(unittest.TestCase):
    """验证系统遮罩不会被误判为 App 页面找不到设备。"""

    def test_locked_phone_is_rejected_before_running_workflow(self) -> None:
        """检测到 Keyguard 时，必须提示用户解锁而不是继续点击页面。"""
        adb = Adb(serial=None)
        with patch.object(adb, "run", return_value="mIsShowing=true"):
            with self.assertRaisesRegex(AutomationError, "已锁屏"):
                adb.require_unlocked()


class DeviceSearchTests(unittest.TestCase):
    """验证首页首屏没有设备时会继续滑动搜索。"""

    def test_searches_after_swiping_up(self) -> None:
        """目标设备出现在第二屏时，应滑动、点击并等待页面加载。"""
        home_without_target = parse_snapshot(
            "<hierarchy><node text='' class='android.widget.FrameLayout' bounds='[0,0][100,200]'><node text='other' class='android.widget.TextView' bounds='[0,0][50,20]' /></node></hierarchy>"
        )
        home_with_target = parse_snapshot(
            "<hierarchy><node text='' class='android.widget.FrameLayout' bounds='[0,0][100,200]'><node text='target' class='android.widget.TextView' bounds='[0,120][100,160]' /></node></hierarchy>"
        )
        device_page = parse_snapshot(
            "<hierarchy><node text='' class='android.widget.FrameLayout' bounds='[0,0][100,200]'><node text='target' class='android.widget.TextView' bounds='[0,0][50,20]' /><node text='模式' class='android.widget.TextView' bounds='[0,30][50,50]' /></node></hierarchy>"
        )

        class FakeAdb:
            def __init__(self) -> None:
                self.snapshots = iter([home_without_target, home_without_target, home_with_target, device_page])
                self.swipes: list[tuple[tuple[int, int], tuple[int, int]]] = []
                self.taps: list[tuple[int, int]] = []

            def dump_ui(self):
                return next(self.snapshots)

            def start_app(self, package: str, activity: str) -> None:
                del package, activity

            def swipe(self, start: tuple[int, int], end: tuple[int, int]) -> None:
                self.swipes.append((start, end))

            def tap(self, x: int, y: int) -> None:
                self.taps.append((x, y))

        adb = FakeAdb()
        home = SmartLifeHome(adb, "package", "activity", timeout=1, poll_interval=0, device_search_max_swipes=2)
        self.assertFalse(home.ensure_device_page("target", lambda snapshot: any(node.text == "模式" for node in snapshot.nodes())))
        self.assertEqual(len(adb.swipes), 1)
        self.assertEqual(adb.taps, [(50, 140)])


if __name__ == "__main__":
    unittest.main()
