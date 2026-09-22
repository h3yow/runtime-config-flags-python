"""运行期配置管理器：原子快照替换、热更新、并发安全。"""

from __future__ import annotations

from typing import Any


class ConfigManager:
    def __init__(self, schema: Any, file_path: str | None = None, env_prefix: str = "APP_") -> None:
        raise NotImplementedError

    def current(self) -> Any:
        raise NotImplementedError

    def reload(self) -> Any:
        raise NotImplementedError
