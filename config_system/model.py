"""不可变配置快照：一个配置版本。

快照一旦构建即不可变（frozen dataclass + 内部字典不外泄修改入口），
运行期更新通过整体替换快照实现，请求侧要么看到完整旧版本、要么看到完整新版本。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from .rollout import in_rollout, stable_bucket

REDACTED = "***REDACTED***"


@dataclasses.dataclass(frozen=True)
class FlagState:
    name: str
    enabled: bool
    percentage: int  # 0-100 启用灰度；-1 表示纯布尔开关


@dataclasses.dataclass(frozen=True)
class ConfigSnapshot:
    version: str
    fields: dict[str, Any]
    flags: dict[str, FlagState]
    salt: str
    secret_keys: frozenset[str]

    def get(self, key: str, default: Any = None) -> Any:
        """读取字段值（服务内部使用，密钥原样返回）。"""
        return self.fields.get(key, default)

    def is_enabled(self, flag: str, subject: str | None = None) -> bool:
        return self.decide(flag, subject)["enabled"]

    def decide(self, flag: str, subject: str | None = None) -> dict[str, Any]:
        """返回开关判定结果与判定依据（可解释）。"""
        state = self.flags.get(flag)
        base = {"version": self.version, "flag": flag}
        if state is None:
            return {**base, "enabled": False, "reason": "unknown_flag"}
        if state.percentage >= 0:
            subj = subject if subject is not None else ""
            bucket = stable_bucket(flag, subj, self.salt)
            on = in_rollout(flag, subj, self.salt, state.percentage)
            return {
                **base,
                "enabled": on,
                "reason": "rollout_in" if on else "rollout_out",
                "bucket": bucket,
                "percentage": state.percentage,
                "subject": subj,
            }
        return {
            **base,
            "enabled": state.enabled,
            "reason": "boolean_on" if state.enabled else "boolean_off",
        }

    def public_view(self) -> dict[str, Any]:
        """对外视图：密钥字段一律脱敏，绝不明文输出。"""
        fields = {
            key: (REDACTED if key in self.secret_keys else value)
            for key, value in self.fields.items()
        }
        flags = {
            name: {"enabled": st.enabled, "percentage": st.percentage}
            for name, st in self.flags.items()
        }
        return {"version": self.version, "fields": fields, "flags": flags}
