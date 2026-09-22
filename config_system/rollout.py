"""按比例灰度分组：稳定、可复现、比例平滑。"""

from __future__ import annotations


def stable_bucket(flag: str, subject: str, salt: str) -> int:
    """返回 0-99 的稳定桶位。"""
    raise NotImplementedError


def in_rollout(flag: str, subject: str, salt: str, percentage: int) -> bool:
    raise NotImplementedError
