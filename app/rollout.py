"""灰度分组：稳定哈希分桶。

设计要点：
- 桶号只取决于 (subject, salt)，与放量比例无关。调整 percentage 时，
  只有跨边界的少量主体改变归属，不会大面积重新分组。
- salt 不绑定配置版本，因此同一主体在各版本间（salt 未改时）归属稳定；
  salt 可在配置中显式更换，用于需要主动重新洗牌的场景。
"""

from __future__ import annotations

import hashlib

MAX_BUCKET = 10000


def stable_bucket(subject: str, salt: str) -> int:
    """返回 [0, MAX_BUCKET) 的稳定桶号。

    同一 (subject, salt) 永远落在同一桶（纯函数、可复现）。
    """
    digest = hashlib.sha256(f"{salt}\x1f{subject}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % MAX_BUCKET


def evaluate_percentage(bucket: int, percentage: int) -> bool:
    """桶号是否落在放量比例内。

    边界：percentage=0 全关；percentage=100 全开；
    采用 bucket < percentage * (MAX_BUCKET/100) 的半开区间。
    """
    if not (0 <= percentage <= 100):
        raise ValueError(f"percentage must be in [0, 100], got {percentage}")
    return bucket < percentage * (MAX_BUCKET // 100)
