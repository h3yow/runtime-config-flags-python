"""不可变配置快照：一个配置版本。

快照一旦构建即不可变（frozen dataclass + 内部字典不外泄修改入口），
运行期更新通过整体替换快照实现，请求侧要么看到完整旧版本、要么看到完整新版本。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from .rollout import stable_bucket

REDACTED = "***REDACTED***"


@dataclasses.dataclass(frozen=True)
class FlagState:
    name: str
    enabled: bool
    percentage: int  # 0-100 启用灰度；-1 表示纯布尔开关
    # 灰度模式下的强制名单：命中与比例无关；同时命中两名单时以关闭为准。
    force_on: frozenset[str] = frozenset()
    force_off: frozenset[str] = frozenset()
    # 一键全量关：灰度模式下优先级最高，对所有主体一律关闭。
    kill_switch: bool = False


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

        灰度模式（percentage >= 0）优先级从高到低：
        1. kill_switch 整体关闭：对所有主体一律关闭（reason=kill_switch）；
        2. force_off 强制关闭名单：命中即关（同时在两个名单里也以关闭为准）；
        3. force_on 强制打开名单：命中即开；
        4. 稳定桶位比例判定（reason=rollout_in/rollout_out）。
        名单命中与比例无关，比例调整只影响名单外主体且只能进不能出。
        """
        state = self.flags.get(flag)
        base = {"version": self.version, "flag": flag}
        if state is None:
            return {**base, "enabled": False, "reason": "unknown_flag"}
        if state.percentage < 0:
            return {
                **base,
                "enabled": state.enabled,
                "reason": "boolean_on" if state.enabled else "boolean_off",
            }
        subj = subject if subject is not None else ""
        # 1. 整体关闭优先级最高：忽略比例与名单
        if state.kill_switch:
            return {
                **base, "enabled": False, "reason": "kill_switch",
                "percentage": state.percentage, "subject": subj,
            }
        # 2/3. 强制名单：关闭优先于打开
        if subj in state.force_off:
            return {
                **base, "enabled": False, "reason": "force_off",
                "percentage": state.percentage, "subject": subj,
            }
        if subj in state.force_on:
            return {
                **base, "enabled": True, "reason": "force_on",
                "percentage": state.percentage, "subject": subj,
            }
        # 4. 比例桶位
        bucket = stable_bucket(flag, subj, self.salt)
        on = bucket < state.percentage
        return {
            **base,
            "enabled": on,
            "reason": "rollout_in" if on else "rollout_out",
            "bucket": bucket,
            "percentage": state.percentage,
            "subject": subj,
        }

    def decide_batch(self, flag: str, subjects: list[str]) -> dict[str, Any]:
        """批量判定：整批基于同一快照（同一配置版本）出结果。

        返回 ``{"version", "flag", "results"}``；results 与入参顺序一一对应。
        空批次返回空 results；重复主体按出现位置各返回一条（结果必然一致），
        内部按主体缓存桶位/判定，重复主体不会重复计算哈希。
        """
        results: list[dict[str, Any]] = []
        cache: dict[str, dict[str, Any]] = {}
        for subject in subjects:
            if subject not in cache:
                cache[subject] = self.decide(flag, subject)
            results.append(cache[subject])
        return {"version": self.version, "flag": flag, "results": results}

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
                "force_on": sorted(st.force_on),
                "force_off": sorted(st.force_off),
                "kill_switch": st.kill_switch,
            }
            for name, st in self.flags.items()
        }
        return {"version": self.version, "fields": fields, "flags": flags}
