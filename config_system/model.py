"""不可变配置快照（一个配置版本）。"""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True)
class FlagState:
    name: str
    enabled: bool
    percentage: int  # 0-100；<0 视为纯布尔开关


@dataclasses.dataclass(frozen=True)
class ConfigSnapshot:
    version: str
    fields: dict[str, Any]
    flags: dict[str, FlagState]
    salt: str
    secret_keys: frozenset[str]

    def get(self, key: str, default: Any = None) -> Any:
        raise NotImplementedError

    def is_enabled(self, flag: str, subject: str | None = None) -> bool:
        raise NotImplementedError

    def decide(self, flag: str, subject: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def public_view(self) -> dict[str, Any]:
        raise NotImplementedError
