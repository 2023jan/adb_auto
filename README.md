# ADB App Automation

通过 ADB 驱动 Android 手机上的智能生活。当前已实现 TRV PID 阀门循环：只有页面上报的阀门开度实际变化时才计为一次动作。后续场景从同一入口扩展，不需要新增 runner。

## 初次准备

需要 macOS、已连接且授权 USB 调试的 Android 手机、`adb` 和 `uv`。手机保持屏幕唤醒、解锁并已登录智能生活；启动时会预检锁屏状态，锁屏会直接停止并留下证据。

```zsh
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
```

## 配置与运行

所有可变参数在 [config](config) 目录中，修改后不需要传入命令行参数。

```text
config/
├── run.yaml                       # 选择本次运行的场景和设备实例
├── lab.yaml                       # App、首页搜索滑动、ADB 序列号、证据输出目录
├── devices/705z_1.yaml            # 智能生活中的设备实例名称
└── scenarios/trv_valve_cycle.yaml # 阀门循环次数、模式和超时
```

日常切换设备或场景时修改 `config/run.yaml`；调整当前设备显示名时修改 `config/devices/705z_1.yaml`；调整循环次数和模式时修改对应 `config/scenarios/*.yaml`。

```zsh
./run.zsh
```

`config/scenarios/trv_valve_cycle.yaml` 中的 `close_mode` 设温必须低于室温，`open_mode` 的设温必须高于室温。试跑将 `moves` 设为 `4`；正式测试按需要设为 `80` 或其他次数。

## 代码结构

```text
main.py                     # 唯一入口：读取 scenario、创建运行目录、统一失败处理
core/                       # ADB、UI XML、配置、等待、日志和证据
pages/                      # 智能生活首页、TRV 小程序页面动作和状态解析
workflows/                  # 按业务场景编排，例如 trv_valve_cycle
```

新增配网、上行同步等场景时，在 `workflows/` 新增业务流程、在 `config/scenarios/` 新增对应 YAML，并在 `workflows/__init__.py` 注册到 `run.scenario`；ADB、页面动作、重试和证据能力继续复用。

## 运行判定

脚本从 PID 顶部状态栏的结构读取状态：左侧文字为当前模式，第一枚百分比为阀门开度，第二枚百分比为电量。因此可以从“自定义”、节能、舒适或其他预设模式启动。

每次运行的 JSONL 日志、截图和 UI XML 保存至 `trv-runs/<时间戳>/`。超时、on/off 模式、页面未完整加载或设温无法驱动预期开关时，脚本会停止且留下证据。
