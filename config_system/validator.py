"""分层合并与类型/取值校验。"""

from __future__ import annotations

from typing import Any

from .errors import ConfigRejectionError


def merge_layers(*layers: dict[str, Any]) -> dict[str, Any]:
    raise NotImplementedError


def validate(raw: dict[str, Any], schema: Any) -> dict[str, Any]:
    raise NotImplementedError


def build_config(raw: dict[str, Any], schema: Any) -> Any:
    raise NotImplementedError
