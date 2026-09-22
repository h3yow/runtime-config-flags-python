"""原始配置的强类型校验。

类型不符、非法取值或缺少必填项都不做静默回退：
所有问题被收集为带类别前缀的拒绝原因（reason），
最终以 ConfigValidationError 一次性抛出，调用方据此保留旧版本。

原因类别（可用前缀区分）：
  MISSING_REQUIRED  缺少必填项
  TYPE_MISMATCH     类型不符
  INVALID_VALUE     类型正确但取值非法
"""

from __future__ import annotations

from typing import Any

from app.errors import ConfigValidationError
from app.model import ConfigSnapshot, FeatureFlag, RolloutRule, Settings

MAX_CONNECTIONS_RANGE = (1, 100_000)
REQUEST_TIMEOUT_MS_RANGE = (1, 600_000)
PERCENTAGE_RANGE = (0, 100)


def _type_name(value: Any) -> str:
    return type(value).__name__


def _check_str(reasons: list[str], data: dict[str, Any], key: str, path: str) -> str | None:
    if key not in data:
        reasons.append(f"MISSING_REQUIRED: {path}")
        return None
    value = data[key]
    if not isinstance(value, str):
        reasons.append(f"TYPE_MISMATCH: {path} expected str, got {_type_name(value)}")
        return None
    if not value.strip():
        reasons.append(f"INVALID_VALUE: {path} must be a non-empty string")
        return None
    return value


def _check_int(
    reasons: list[str],
    data: dict[str, Any],
    key: str,
    path: str,
    bounds: tuple[int, int],
) -> int | None:
    if key not in data:
        reasons.append(f"MISSING_REQUIRED: {path}")
        return None
    value = data[key]
    # bool 是 int 的子类，必须显式排除，避免 true 被当成 1 静默通过
    if isinstance(value, bool) or not isinstance(value, int):
        reasons.append(f"TYPE_MISMATCH: {path} expected int, got {_type_name(value)}")
        return None
    lo, hi = bounds
    if not (lo <= value <= hi):
        reasons.append(f"INVALID_VALUE: {path} must be in [{lo}, {hi}], got {value}")
        return None
    return value


def _check_bool(
    reasons: list[str], data: dict[str, Any], key: str, path: str, *, required: bool
) -> bool | None:
    if key not in data:
        if required:
            reasons.append(f"MISSING_REQUIRED: {path}")
        return None
    value = data[key]
    if not isinstance(value, bool):
        reasons.append(f"TYPE_MISMATCH: {path} expected bool, got {_type_name(value)}")
        return None
    return value


def _validate_settings(raw: dict[str, Any], reasons: list[str]) -> Settings | None:
    section = raw.get("settings")
    if section is None:
        reasons.append("MISSING_REQUIRED: settings")
        return None
    if not isinstance(section, dict):
        reasons.append(f"TYPE_MISMATCH: settings expected object, got {_type_name(section)}")
        return None

    service_name = _check_str(reasons, section, "service_name", "settings.service_name")
    max_connections = _check_int(
        reasons, section, "max_connections", "settings.max_connections", MAX_CONNECTIONS_RANGE
    )
    request_timeout_ms = _check_int(
        reasons,
        section,
        "request_timeout_ms",
        "settings.request_timeout_ms",
        REQUEST_TIMEOUT_MS_RANGE,
    )
    enable_cache = _check_bool(
        reasons, section, "enable_cache", "settings.enable_cache", required=False
    )
    api_base_url = _check_str(reasons, section, "api_base_url", "settings.api_base_url")
    secret_token = _check_str(reasons, section, "secret_token", "settings.secret_token")

    if reasons:
        return None
    return Settings(
        service_name=service_name,  # type: ignore[arg-type]
        max_connections=max_connections,  # type: ignore[arg-type]
        request_timeout_ms=request_timeout_ms,  # type: ignore[arg-type]
        enable_cache=True if enable_cache is None else enable_cache,
        api_base_url=api_base_url,  # type: ignore[arg-type]
        secret_token=secret_token,  # type: ignore[arg-type]
    )


def _validate_flags(raw: dict[str, Any], reasons: list[str]) -> dict[str, FeatureFlag]:
    section = raw.get("flags")
    if section is None:
        return {}
    if not isinstance(section, dict):
        reasons.append(f"TYPE_MISMATCH: flags expected object, got {_type_name(section)}")
        return {}

    flags: dict[str, FeatureFlag] = {}
    for name, spec in section.items():
        path = f"flags.{name}"
        if not isinstance(spec, dict):
            reasons.append(f"TYPE_MISMATCH: {path} expected object, got {_type_name(spec)}")
            continue

        before = len(reasons)
        enabled = _check_bool(reasons, spec, "enabled", f"{path}.enabled", required=True)

        rollout: RolloutRule | None = None
        rollout_raw = spec.get("rollout")
        if rollout_raw is not None:
            if not isinstance(rollout_raw, dict):
                reasons.append(
                    f"TYPE_MISMATCH: {path}.rollout expected object, got {_type_name(rollout_raw)}"
                )
            else:
                percentage = _check_int(
                    reasons,
                    rollout_raw,
                    "percentage",
                    f"{path}.rollout.percentage",
                    PERCENTAGE_RANGE,
                )
                salt_raw = rollout_raw.get("salt")
                salt: str | None = None
                if salt_raw is not None:
                    if not isinstance(salt_raw, str) or not salt_raw.strip():
                        reasons.append(
                            f"INVALID_VALUE: {path}.rollout.salt must be a non-empty string"
                        )
                    else:
                        salt = salt_raw
                if len(reasons) == before and percentage is not None:
                    # 未显式提供 salt 时用开关名，保证分组稳定且不同开关互不干扰
                    rollout = RolloutRule(percentage=percentage, salt=salt or str(name))

        if len(reasons) == before and enabled is not None:
            flags[str(name)] = FeatureFlag(name=str(name), enabled=enabled, rollout=rollout)
    return flags


def validate(raw: dict[str, Any], *, version: int) -> ConfigSnapshot:
    """把合并后的原始字典校验并转换为不可变快照。

    任何问题均抛出 ConfigValidationError，reasons 中逐条说明。
    """
    if not isinstance(raw, dict):
        raise ConfigValidationError(
            "config root must be an object",
            reasons=[f"TYPE_MISMATCH: <root> expected object, got {_type_name(raw)}"],
        )

    reasons: list[str] = []
    settings = _validate_settings(raw, reasons)
    flags = _validate_flags(raw, reasons)

    if reasons:
        raise ConfigValidationError(
            f"config validation failed with {len(reasons)} reason(s)", reasons=reasons
        )
    assert settings is not None
    return ConfigSnapshot(version=version, settings=settings, flags=flags)
