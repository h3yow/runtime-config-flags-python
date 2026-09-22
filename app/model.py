"""配置领域模型与类型定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FlagDecision(str, Enum):
    """特性开关判定结果。"""

    ON = "on"
    OFF = "off"


@dataclass(frozen=True)
class Settings:
    """已校验的强类型配置项。"""

    service_name: str = ""
    max_connections: int = 0
    request_timeout_ms: int = 0
    enable_cache: bool = False
    api_base_url: str = ""
    secret_token: str = ""


@dataclass(frozen=True)
class RolloutRule:
    """灰度规则：按百分比放量，基于稳定哈希分组。"""

    percentage: int = 0
    salt: str = ""


@dataclass(frozen=True)
class FeatureFlag:
    """单个特性开关。"""

    name: str = ""
    enabled: bool = False
    rollout: RolloutRule | None = None


@dataclass(frozen=True)
class ConfigSnapshot:
    """某一不可变配置版本（请求在其整个生命周期内看到的就是它）。"""

    version: int = 0
    settings: Settings = field(default_factory=Settings)
    flags: dict[str, FeatureFlag] = field(default_factory=dict)
