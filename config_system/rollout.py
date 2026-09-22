"""按比例灰度分组：稳定、可复现、比例平滑。

分组规则
--------
对 ``salt|flag|subject`` 做 SHA-256，取前 8 位十六进制映射到 0-99 的桶位::

    bucket = sha256("{salt}|{flag}|{subject}") % 100

特性开关开启灰度时，桶位 ``< percentage`` 的主体落在“开启”组。

* 同一 (salt=配置版本盐、flag、subject) 的结果恒定，不会漂移；
* 以“桶位 < 比例”判定，比例从 p1 调大到 p2 时，新加入的是桶位
  ``[p1, p2)`` 的主体，已开启的主体不会被踢出，避免大面积重新分组；
* salt 进入配置版本快照，配置版本不变则分组绝对稳定。
"""

from __future__ import annotations

import hashlib


def stable_bucket(flag: str, subject: str, salt: str) -> int:
    """返回该主体在某开关下的稳定桶位（0-99，共 100 桶）。"""
    material = f"{salt}|{flag}|{subject}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    # 取前 64 bit 取模，分布均匀且实现稳定（不依赖 PYTHONHASHSEED）。
    value = int(digest[:16], 16)
    return value % 100


def in_rollout(flag: str, subject: str, salt: str, percentage: int) -> bool:
    """桶位 < percentage 即落在灰度开启组。

    percentage <= 0 恒为 False；>= 100 恒为 True。
    """
    if percentage <= 0:
        return False
    if percentage >= 100:
        return True
    return stable_bucket(flag, subject, salt) < percentage
