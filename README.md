# ADB TRV Valve Cycle

通过 ADB 驱动 Android 手机上的智能生活，循环切换 TRV 的 PID 预设模式；只有页面上报的阀门开度实际变化时才计为一次动作。

## 初次准备

需要 macOS、已连接且授权 USB 调试的 Android 手机、`adb` 和 `uv`。手机保持解锁并已登录智能生活。

```zsh
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
```

## 配置与运行

所有可变参数都在 [config.yaml](config.yaml)：设备名称、动作次数、超时、轮询周期、关阀模式、开阀模式和 ADB 序列号。修改后不需要传入命令行参数。

```zsh
./run_trv_valve_cycle.zsh
```

`cycle.close_mode` 的设温必须低于室温，`cycle.open_mode` 的设温必须高于室温。试跑将 `moves` 设为 `4`；正式测试按需要设为 `80` 或其他次数。

## 运行判定

脚本从 PID 顶部状态栏的结构读取状态：左侧文字为当前模式，第一枚百分比为阀门开度，第二枚百分比为电量。因此可以从“自定义”、节能、舒适或其他预设模式启动。

每次运行的 JSONL 日志、截图和 UI XML 保存至 `trv-runs/<时间戳>/`。超时、on/off 模式、页面未完整加载或设温无法驱动预期开关时，脚本会停止且留下证据。
