#!/bin/zsh
# 唯一启动器：业务场景和所有运行参数均由 config/ 指定。
if (( $# != 0 )); then
  print -u2 "不支持命令行参数。请修改同目录 config/ 中的 YAML 后再执行。"
  exit 2
fi

script_dir=${0:A:h}
cd "$script_dir" || exit 1
exec "$script_dir/.venv/bin/python" -m main
