#!/usr/bin/env python3
"""兼容旧的直接执行方式；新入口请使用 ``./run.zsh``。"""

from main import main


if __name__ == "__main__":
    raise SystemExit(main())
