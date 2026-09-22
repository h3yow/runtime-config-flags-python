"""配置来源：默认值、文件、环境变量。"""

from __future__ import annotations

from typing import Any


def defaults_layer(schema: Any) -> dict[str, Any]:
    raise NotImplementedError


def load_file_layer(path: str) -> dict[str, Any]:
    raise NotImplementedError


def load_env_layer(prefix: str = "APP_") -> dict[str, Any]:
    raise NotImplementedError
