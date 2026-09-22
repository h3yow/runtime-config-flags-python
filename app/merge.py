"""分层合并逻辑。

优先级固定且稳定：传入层列表时按"从低到高"排列，
后者覆盖前者（defaults < file < env）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MergeResult:
    """合并结果。

    data: 合并后的原始配置；
    provenance: 每个叶子键的点分路径 -> 提供该最终值的来源名。
    """

    data: dict[str, Any]
    provenance: dict[str, str]


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并两个字典，override 优先；不修改入参。

    只有两边同为 dict 的键才递归下钻，其余情况整体覆盖
    （列表按整体替换，不做元素级合并）。
    """
    result: dict[str, Any] = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _leaf_paths(data: dict[str, Any], prefix: str = "") -> list[str]:
    paths: list[str] = []
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            paths.extend(_leaf_paths(value, path))
        else:
            paths.append(path)
    return paths


def merge_layers(layers: list[tuple[str, dict[str, Any]]]) -> MergeResult:
    """按优先级从低到高合并各层，并记录每个最终值来自哪一层。"""
    merged: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for source_name, layer in layers:
        merged = deep_merge(merged, layer)
        # 后处理的层优先级更高，直接覆盖溯源记录
        for path in _leaf_paths(layer):
            provenance[path] = source_name
    return MergeResult(data=merged, provenance=provenance)
