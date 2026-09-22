"""配置管理器：持有当前不可变版本，支持原子热更新。

并发模型：
- 每个版本是不可变 ConfigSnapshot；管理器只持有一个引用。
- reload 先在"旧版本之外"完成全部校验，构造好新快照后才在锁内
  做一次引用替换；校验失败则绝不触碰当前引用。
- 请求先取一次快照引用，之后整次判定都基于该引用，
  因此只能看到完整的旧版本或新版本，不会看到中间态。
"""

from __future__ import annotations

import threading
from typing import Any

from app.errors import ConfigError
from app.model import ConfigSnapshot, FlagDecision
from app.rollout import MAX_BUCKET, evaluate_percentage, stable_bucket
from app.validation import validate

REDACTED = "***redacted***"


class ConfigManager:
    """线程安全的配置版本持有者。"""

    def __init__(self) -> None:
        self._snapshot: ConfigSnapshot | None = None
        self._lock = threading.RLock()

    def current(self) -> ConfigSnapshot:
        """获取当前版本快照（永不返回半成品）。"""
        snapshot = self._snapshot  # 单条属性读取在 CPython 下是原子的
        if snapshot is None:
            raise ConfigError("no config version available yet")
        return snapshot

    def load_initial(self, raw: dict[str, Any]) -> ConfigSnapshot:
        """首次加载；校验失败直接抛出，不产生任何版本。"""
        candidate = validate(raw, version=1)
        with self._lock:
            self._snapshot = candidate
        return candidate

    def reload(self, raw: dict[str, Any]) -> ConfigSnapshot:
        """原子热更新。

        先完整校验并构造候选快照，成功后才在锁内替换引用；
        失败抛出 ConfigValidationError，当前版本保持不变。
        """
        with self._lock:
            base = self._snapshot
            if base is None:
                raise ConfigError("manager is uninitialized; call load_initial first")
            candidate = validate(raw, version=base.version + 1)
            self._snapshot = candidate  # 唯一的写操作：整引用替换
            return candidate

    def decide(self, flag_name: str, subject: str | None = None) -> tuple[FlagDecision, str]:
        """判定开关，返回 (结果, 判定依据说明)。"""
        return self.decide_snapshot(self.current(), flag_name, subject)

    def decide_snapshot(
        self, snapshot: ConfigSnapshot, flag_name: str, subject: str | None = None
    ) -> tuple[FlagDecision, str]:
        """基于指定快照判定。请求先取一次快照再调用本方法，

        可保证响应中的版本号与判定依据来自同一个完整版本。
        """
        version = snapshot.version
        flag = snapshot.flags.get(flag_name)

        if flag is None:
            return (
                FlagDecision.OFF,
                f"version={version} flag={flag_name} reason=flag_unknown action=off",
            )
        if not flag.enabled:
            return (
                FlagDecision.OFF,
                f"version={version} flag={flag_name} reason=flag_disabled action=off",
            )
        if flag.rollout is None:
            return (
                FlagDecision.ON,
                f"version={version} flag={flag_name} reason=flag_enabled action=on",
            )

        percentage = flag.rollout.percentage
        salt = flag.rollout.salt
        if percentage <= 0:
            return (
                FlagDecision.OFF,
                f"version={version} flag={flag_name} reason=rollout_zero_percent action=off",
            )
        if percentage >= 100:
            return (
                FlagDecision.ON,
                f"version={version} flag={flag_name} reason=rollout_full_percent action=on",
            )
        if subject is None:
            return (
                FlagDecision.OFF,
                f"version={version} flag={flag_name} reason=subject_required_for_rollout "
                f"percentage={percentage} action=off",
            )

        bucket = stable_bucket(subject, salt)
        threshold = percentage * (MAX_BUCKET // 100)
        hit = evaluate_percentage(bucket, percentage)
        decision = FlagDecision.ON if hit else FlagDecision.OFF
        reason = (
            f"version={version} flag={flag_name} reason=rollout_bucket subject={subject} "
            f"salt={salt} percentage={percentage} bucket={bucket} threshold={threshold} "
            f"hit={'true' if hit else 'false'} action={decision.value}"
        )
        return decision, reason

    def public_view(self) -> dict[str, Any]:
        """对外视图：密钥字段一律脱敏，绝不输出明文。"""
        snapshot = self.current()
        settings = snapshot.settings
        return {
            "version": snapshot.version,
            "settings": {
                "service_name": settings.service_name,
                "max_connections": settings.max_connections,
                "request_timeout_ms": settings.request_timeout_ms,
                "enable_cache": settings.enable_cache,
                "api_base_url": settings.api_base_url,
                "secret_token": REDACTED,
            },
            "flags": {
                name: {
                    "enabled": flag.enabled,
                    "rollout": None
                    if flag.rollout is None
                    else {"percentage": flag.rollout.percentage, "salt": flag.rollout.salt},
                }
                for name, flag in snapshot.flags.items()
            },
        }
