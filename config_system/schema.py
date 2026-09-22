"""字段与特性开关的类型规格定义。"""

from __future__ import annotations

import dataclasses
from typing import Any, Literal


@dataclasses.dataclass(frozen=True)
class FieldSpec:
    key: str
    type: Literal["str", "int", "float", "bool", "secret"]
    required: bool = False
    default: Any = None
    # 取值约束
    choices: tuple[Any, ...] | None = None
    min_value: float | None = None
    max_value: float | None = None


@dataclasses.dataclass(frozen=True)
class FlagSpec:
    name: str
    default: bool = False


@dataclasses.dataclass(frozen=True)
class ConfigSchema:
    fields: tuple[FieldSpec, ...] = ()
    flags: tuple[FlagSpec, ...] = ()
