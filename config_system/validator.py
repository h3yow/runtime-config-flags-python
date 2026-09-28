"""分层合并与校验：所有拒绝都携带可区分原因，绝不静默回退。

合并顺序（后者覆盖前者）：defaults -> file -> env。
校验在合并后的整体视图上进行：

* 未知字段/未知开关            -> INVALID_KEY
* 类型不符（含 bool/int 混淆） -> TYPE_MISMATCH
* 取值越界/不在枚举/比例越界   -> ILLEGAL_VALUE
* 必填缺失                     -> MISSING_REQUIRED
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .errors import ConfigRejectionError, RejectionReason
from .model import ConfigSnapshot, FlagState
from .schema import ConfigSchema, FieldSpec

# 部署级盐：保证同一主体在不同配置版本间分组稳定（比例平滑调整的前提）。
DEFAULT_SALT = "runtime-config-flags"


def merge_layers(*layers: dict[str, Any]) -> dict[str, Any]:
    """按给定顺序深合并各层，后者覆盖前者。"""
    merged: dict[str, Any] = {"fields": {}, "flags": {}}
    for layer in layers:
        merged["fields"].update(layer.get("fields", {}))
        for name, flag_val in layer.get("flags", {}).items():
            merged["flags"].setdefault(name, {}).update(flag_val)
    return merged


def validate(raw: dict[str, Any], schema: ConfigSchema) -> dict[str, Any]:
    """校验合并后的原始配置，返回规范化结果；失败抛 ConfigRejectionError。"""
    fields = _validate_fields(raw.get("fields", {}), schema)
    flags = _validate_flags(raw.get("flags", {}), schema)
    return {"fields": fields, "flags": flags}


def build_config(raw: dict[str, Any], schema: ConfigSchema, salt: str = DEFAULT_SALT) -> ConfigSnapshot:
    """校验并构建不可变配置快照，版本号为内容哈希。"""
    validated = validate(raw, schema)
    fields = validated["fields"]
    flags = validated["flags"]
    canonical = json.dumps(
        {"fields": _jsonable(fields), "flags": {k: _jsonable(dataclasses_asdict(v)) for k, v in flags.items()}},
        sort_keys=True,
    )
    version = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    secret_keys = frozenset(spec.key for spec in schema.fields if spec.type == "secret")
    return ConfigSnapshot(
        version=version,
        fields=fields,
        flags=flags,
        salt=salt,
        secret_keys=secret_keys,
    )


def dataclasses_asdict(obj: Any) -> dict[str, Any]:
    import dataclasses
    return dataclasses.asdict(obj)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    # 名单内部为 frozenset：规范化为排序后的列表，保证哈希稳定可复现
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _validate_fields(raw_fields: dict[str, Any], schema: ConfigSchema) -> dict[str, Any]:
    specs = {spec.key: spec for spec in schema.fields}
    for key in raw_fields:
        if key not in specs:
            raise ConfigRejectionError(
                RejectionReason.INVALID_KEY, f"fields.{key}", "未在 schema 中声明的字段")
    out: dict[str, Any] = {}
    for key, spec in specs.items():
        if key not in raw_fields:
            if spec.required:
                raise ConfigRejectionError(
                    RejectionReason.MISSING_REQUIRED, f"fields.{key}", "必填字段缺失")
            continue
        out[key] = _check_field(key, raw_fields[key], spec)
    return out


def _check_field(key: str, value: Any, spec: FieldSpec) -> Any:
    path = f"fields.{key}"
    expected = spec.type
    if expected == "bool":
        if not isinstance(value, bool):
            raise ConfigRejectionError(RejectionReason.TYPE_MISMATCH, path,
                                       f"期望 bool，实际 {type(value).__name__}")
    elif expected == "int":
        # bool 是 int 子类，必须显式排除，避免 true 被当成 1
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigRejectionError(RejectionReason.TYPE_MISMATCH, path,
                                       f"期望 int，实际 {type(value).__name__}")
    elif expected == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigRejectionError(RejectionReason.TYPE_MISMATCH, path,
                                       f"期望 float，实际 {type(value).__name__}")
        value = float(value)
    elif expected in ("str", "secret"):
        if not isinstance(value, str):
            raise ConfigRejectionError(RejectionReason.TYPE_MISMATCH, path,
                                       f"期望 str，实际 {type(value).__name__}")
    if spec.choices is not None and value not in spec.choices:
        raise ConfigRejectionError(RejectionReason.ILLEGAL_VALUE, path,
                                   f"取值 {value!r} 不在允许集合 {spec.choices!r} 内")
    if spec.min_value is not None and isinstance(value, (int, float)) and value < spec.min_value:
        raise ConfigRejectionError(RejectionReason.ILLEGAL_VALUE, path,
                                   f"取值 {value!r} 小于下限 {spec.min_value}")
    if spec.max_value is not None and isinstance(value, (int, float)) and value > spec.max_value:
        raise ConfigRejectionError(RejectionReason.ILLEGAL_VALUE, path,
                                   f"取值 {value!r} 大于上限 {spec.max_value}")
    return value


def _validate_flags(raw_flags: dict[str, Any], schema: ConfigSchema) -> dict[str, FlagState]:
    known = {flag.name for flag in schema.flags}
    out: dict[str, FlagState] = {}
    for name in known:
        out[name] = FlagState(name=name, enabled=False, percentage=-1)
    for name, val in raw_flags.items():
        if name not in known:
            raise ConfigRejectionError(
                RejectionReason.INVALID_KEY, f"flags.{name}", "未在 schema 中声明的开关")
        path = f"flags.{name}"
        enabled = val.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ConfigRejectionError(RejectionReason.TYPE_MISMATCH, f"{path}.enabled",
                                       f"期望 bool，实际 {type(enabled).__name__}")
        percentage = val.get("percentage", -1)
        if isinstance(percentage, bool) or not isinstance(percentage, int):
            raise ConfigRejectionError(RejectionReason.TYPE_MISMATCH, f"{path}.percentage",
                                       f"期望 int，实际 {type(percentage).__name__}")
        if percentage != -1 and not (0 <= percentage <= 100):
            raise ConfigRejectionError(RejectionReason.ILLEGAL_VALUE, f"{path}.percentage",
                                       f"灰度比例 {percentage} 不在 0-100")
        force_on = _validate_subject_list(val.get("force_on", []), f"{path}.force_on")
        force_off = _validate_subject_list(val.get("force_off", []), f"{path}.force_off")
        kill_switch = val.get("kill_switch", False)
        if not isinstance(kill_switch, bool):
            raise ConfigRejectionError(RejectionReason.TYPE_MISMATCH, f"{path}.kill_switch",
                                       f"期望 bool，实际 {type(kill_switch).__name__}")
        out[name] = FlagState(
            name=name,
            enabled=enabled,
            percentage=percentage,
            force_on=force_on,
            force_off=force_off,
            kill_switch=kill_switch,
        )
    return out


def _validate_subject_list(raw: Any, path: str) -> frozenset[str]:
    """校验强制名单：必须是字符串数组；任何元素类型不对都整次拒绝。"""
    if not isinstance(raw, list):
        raise ConfigRejectionError(
            RejectionReason.TYPE_MISMATCH, path,
            f"期望字符串数组，实际 {type(raw).__name__}")
    for item in raw:
        if not isinstance(item, str):
            raise ConfigRejectionError(
                RejectionReason.TYPE_MISMATCH, path,
                f"名单元素必须全部为字符串，发现 {type(item).__name__}: {item!r}")
    return frozenset(raw)
