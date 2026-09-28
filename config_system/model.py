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
    # 一键全量关：True 时对所有主体一律关闭，优先级高于比例与名单
    global_off: bool = False
    # 强制打开/强制关闭名单：命中与比例无关；同时命中两名单时以关闭为准
    force_on: frozenset[str] = frozenset()
    force_off: frozenset[str] = frozenset()


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
        """返回开关判定结果与判定依据（可解释）。

        优先级（高 -> 低）：
          1. 整体关闭 global_off      -> reason=global_off，所有主体一律关闭
          2. 强制关闭名单 force_off   -> reason=force_off（同时命中两名单时以关闭为准）
          3. 强制打开名单 force_on    -> reason=force_on，稳定命中、不受比例影响
          4. 比例灰度 / 纯布尔         -> rollout_in/rollout_out、boolean_on/boolean_off
        """
        state = self.flags.get(flag)
        base = {"version": self.version, "flag": flag}
        if state is None:
            return {**base, "enabled": False, "reason": "unknown_flag"}
        subj = subject if subject is not None else ""
        if state.global_off:
            return {**base, "enabled": False, "reason": "global_off", "subject": subj}
        # 两名单同时命中时以关闭为准，因此 force_off 先于 force_on 判定
        if subj in state.force_off:
            return {**base, "enabled": False, "reason": "force_off", "subject": subj}
        if subj in state.force_on:
            return {**base, "enabled": True, "reason": "force_on", "subject": subj}
        if state.percentage >= 0:
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
            "subject": subj,
        }

    def decide_batch(self, flag: str, subjects: list[str]) -> dict[str, Any]:
        """批量判定：整批基于同一不可变快照（同一配置版本）出结果。

        重复主体按输入位置逐个返回（判定结果必然一致）；空批次返回空 results。
        纯 CPU 哈希判定，单次 O(n)，不存在随批量增长的额外开销。
        """
        return {
            "version": self.version,
            "flag": flag,
            "count": len(subjects),
            "results": [self.decide(flag, s) for s in subjects],
        }

    def public_view(self) -> dict[str, Any]:
        """对外视图：密钥字段一律脱敏，绝不明文输出。"""
        fields = {
            key: (REDACTED if key in self.secret_keys else value)
            for key, value in self.fields.items()
        }
        flags = {
            name: {
                "enabled": st.enabled,
                "percentage": st.percentage,
                "global_off": st.global_off,
                "force_on": sorted(st.force_on),
                "force_off": sorted(st.force_off),
            }
            for name, st in self.flags.items()
        }
        return {"version": self.version, "fields": fields, "flags": flags}
