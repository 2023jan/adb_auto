"""项目统一异常类型。"""


class AutomationError(RuntimeError):
    """测试无法安全继续或无法确认结果时抛出的异常。"""
