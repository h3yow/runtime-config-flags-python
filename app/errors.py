"""配置子系统的错误类型。

不同失败原因使用不同异常类，便于调用方区分拒绝原因。
"""

from __future__ import annotations


class ConfigError(Exception):
    """配置错误基类。"""


class ConfigValidationError(ConfigError):
    """配置校验失败（类型不符 / 非法取值 / 缺少必填项等）。"""

    def __init__(self, message: str, *, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.reasons: list[str] = list(reasons or [])


class ConfigSourceError(ConfigError):
    """读取配置来源失败（文件不存在 / 解析失败 / 环境变量非法等）。"""


class ConfigUpdateRejected(ConfigError):
    """一次热更新被整体拒绝，旧版本继续生效。"""

    def __init__(self, message: str, reasons: list[str]) -> None:
        super().__init__(message)
        self.reasons: list[str] = list(reasons)
