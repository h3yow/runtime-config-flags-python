"""分层配置来源：defaults < file < env（优先级从低到高）。

每个来源只负责"读出原始字典"；类型与取值校验统一交给
app.validation，来源层的失败（文件缺失 / JSON 非法等）
抛出 ConfigSourceError，与校验失败可区分。
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Protocol

from app.errors import ConfigSourceError


class ConfigSource(Protocol):
    """配置来源协议。"""

    name: str

    def load(self) -> dict[str, Any]:
        """读取原始配置字典；读取失败抛出 ConfigSourceError。"""
        ...


class DefaultsSource:
    """内置默认值来源（优先级最低）。"""

    name = "defaults"

    def __init__(self, defaults: dict[str, Any] | None = None) -> None:
        self._defaults = dict(defaults or {})

    def load(self) -> dict[str, Any]:
        return copy.deepcopy(self._defaults)


class FileSource:
    """JSON 配置文件来源。

    optional=False（默认）时文件缺失即报错，避免"以为加载了配置
    实际在用默认值"的静默回退；optional=True 时缺失按空层处理。
    """

    name = "file"

    def __init__(self, path: str, *, optional: bool = False) -> None:
        self._path = path
        self._optional = optional

    def load(self) -> dict[str, Any]:
        if not os.path.exists(self._path):
            if self._optional:
                return {}
            raise ConfigSourceError(f"CONFIG_FILE_NOT_FOUND: {self._path}")
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ConfigSourceError(
                f"CONFIG_FILE_INVALID_JSON: {self._path}: {exc}"
            ) from exc
        except OSError as exc:
            raise ConfigSourceError(f"CONFIG_FILE_UNREADABLE: {self._path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigSourceError(
                f"CONFIG_FILE_NOT_OBJECT: {self._path} root must be a JSON object"
            )
        return data


def _parse_env_value(raw: str) -> Any:
    """尽量把环境变量解析为 JSON 标量，失败则保留原始字符串。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


class EnvironmentSource:
    """环境变量来源（优先级最高）。

    命名约定：{PREFIX}SECTION__KEY，双下划线表示嵌套，如
    APP_SETTINGS__MAX_CONNECTIONS=200 -> settings.max_connections = 200
    """

    name = "env"

    def __init__(self, prefix: str = "APP_", environ: dict[str, str] | None = None) -> None:
        self._prefix = prefix
        self._environ = os.environ if environ is None else environ

    def load(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, raw_value in self._environ.items():
            if not key.startswith(self._prefix):
                continue
            remainder = key[len(self._prefix):]
            if not remainder:
                continue
            parts = [p.lower() for p in remainder.split("__") if p]
            if not parts:
                continue
            node = result
            for part in parts[:-1]:
                existing = node.get(part)
                if not isinstance(existing, dict):
                    existing = {}
                    node[part] = existing
                node = existing
            node[parts[-1]] = _parse_env_value(raw_value)
        return result
