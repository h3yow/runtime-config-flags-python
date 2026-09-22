"""配置来源：默认值层、配置文件层、环境变量层。

优先级（后者覆盖前者）::

    1. schema 中声明的默认值（defaults）
    2. JSON 配置文件（file）
    3. 环境变量（env）

各来源只负责“取出原始数据”，不做类型校验；校验统一在 validator 中完成，
这样类型不符/非法取值/缺必填都能给出可区分的拒绝原因，而不是静默回退。

环境变量约定（前缀可配置，默认 ``APP_``）::

    APP_FIELD_<KEY>=value   -> fields.<key>
    APP_FLAG_<NAME>=true|false  -> flags.<name>.enabled
    APP_ROLLOUT_<NAME>=0-100    -> flags.<name>.percentage

布尔值接受 true/false/1/0/yes/no/on/off（大小写不敏感）。
文件不存在时按空层处理（可选配置）；文件存在但非法则抛 SOURCE_READ_ERROR。
"""

from __future__ import annotations

import json
import os
from typing import Any

from .errors import ConfigRejectionError, RejectionReason
from .schema import ConfigSchema

_BOOL_TRUE = {"true", "1", "yes", "on"}
_BOOL_FALSE = {"false", "0", "no", "off"}


def defaults_layer(schema: ConfigSchema) -> dict[str, Any]:
    """第 1 层：schema 中声明的默认值。无默认值的字段不出现在此层。"""
    fields: dict[str, Any] = {}
    flags: dict[str, Any] = {}
    for spec in schema.fields:
        if spec.default is not None:
            fields[spec.key] = spec.default
    for flag in schema.flags:
        flags[flag.name] = {"enabled": flag.default}
    return {"fields": fields, "flags": flags}


def load_file_layer(path: str) -> dict[str, Any]:
    """第 2 层：JSON 文件。文件不存在返回空层；解析失败显式拒绝。"""
    if not os.path.exists(path):
        return {"fields": {}, "flags": {}}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigRejectionError(
            RejectionReason.SOURCE_READ_ERROR,
            path,
            f"配置文件无法读取或不是合法 JSON: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise ConfigRejectionError(
            RejectionReason.SOURCE_READ_ERROR,
            path,
            "配置文件顶层必须是对象",
        )
    return _normalize_layer(data, source=path)


def load_env_layer(prefix: str = "APP_", environ: dict[str, str] | None = None) -> dict[str, Any]:
    """第 3 层：环境变量，优先级最高。"""
    env = os.environ if environ is None else environ
    fields: dict[str, Any] = {}
    flags: dict[str, Any] = {}
    for raw_key, raw_val in env.items():
        key = raw_key.upper()
        if key.startswith(f"{prefix}FIELD_"):
            name = key[len(prefix) + len("FIELD_"):].lower()
            fields[name] = _coerce_scalar(raw_val)
        elif key.startswith(f"{prefix}ROLLOUT_"):
            name = key[len(prefix) + len("ROLLOUT_"):].lower()
            flags.setdefault(name, {})["percentage"] = _coerce_scalar(raw_val)
        elif key.startswith(f"{prefix}FLAG_"):
            name = key[len(prefix) + len("FLAG_"):].lower()
            flags.setdefault(name, {})["enabled"] = _coerce_scalar(raw_val)
    return {"fields": fields, "flags": flags}


def _coerce_scalar(value: str) -> Any:
    """对环境变量做最小转换：布尔/整数/浮点/字符串。是否合法交给 validator。"""
    stripped = value.strip()
    low = stripped.lower()
    if low in _BOOL_TRUE:
        return True
    if low in _BOOL_FALSE:
        return False
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        pass
    return stripped


def _normalize_layer(data: dict[str, Any], *, source: str) -> dict[str, Any]:
    """允许文件形如 {"fields": {...}, "flags": {...}}，或扁平 {key: value}。

    flags 的值可为布尔量（enabled）或对象 {enabled, percentage}。
    """
    layer = {"fields": {}, "flags": {}}
    fields = data.get("fields")
    flags = data.get("flags")
    if fields is not None:
        if not isinstance(fields, dict):
            raise ConfigRejectionError(RejectionReason.TYPE_MISMATCH, f"{source}:fields",
                                       "fields 必须是对象")
        layer["fields"] = dict(fields)
    if flags is not None:
        if not isinstance(flags, dict):
            raise ConfigRejectionError(RejectionReason.TYPE_MISMATCH, f"{source}:flags",
                                       "flags 必须是对象")
        for name, val in flags.items():
            if isinstance(val, dict):
                layer["flags"][name] = dict(val)
            else:
                layer["flags"][name] = {"enabled": val}
    # 扁平键：既不在 fields/flags 命名空间下的顶层键一律视为字段
    for key, val in data.items():
        if key in ("fields", "flags"):
            continue
        layer["fields"][key] = val
    return layer
