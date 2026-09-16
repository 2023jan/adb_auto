"""TRV 自动化脚本的离线 UI 与配置解析测试，不连接 Android 设备。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from trv_valve_cycle import TrvError, load_config, parse_snapshot, read_header, read_large_setpoint


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
        snapshot = parse_snapshot(PID_CUSTOM_XML.replace("<node text='12%' class='android.widget.TextView' bounds='[515,374][597,427]' />", ""))
        with self.assertRaisesRegex(TrvError, "on/off"):
            read_header(snapshot)


class ConfigTests(unittest.TestCase):
    """验证所有运行参数均从 YAML 读取并被校验。"""

    def test_loads_all_runtime_values_from_yaml(self) -> None:
        """设备名、次数、等待值和模式名均不能依赖脚本硬编码。"""
        content = """device:
  name: other-trv
  serial: serial-1
cycle:
  moves: 4
  timeout_seconds: 15
  poll_interval_seconds: 1
  close_mode: 防冻
  open_mode: 舒适
output:
  runs_dir: evidence
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(content, encoding="utf-8")
            config = load_config(path)
            self.assertEqual(config.device_name, "other-trv")
            self.assertEqual(config.moves, 4)
            self.assertEqual(config.close_mode, "防冻")
            self.assertEqual(config.output_dir, path.parent / "evidence")


if __name__ == "__main__":
    unittest.main()
