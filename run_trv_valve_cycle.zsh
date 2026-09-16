#!/bin/zsh
# 所有测试参数由同目录 config.yaml 提供；此启动器不接收命令行参数。
if (( $# != 0 )); then
  print -u2 "不支持命令行参数。请修改同目录 config.yaml 后再执行。"
  exit 2
fi

script_dir=${0:A:h}
exec "$script_dir/.venv/bin/python" "$script_dir/trv_valve_cycle.py"
