"""把分层来源、校验与管理器组装起来的加载服务。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.errors import ConfigSourceError, ConfigUpdateRejected, ConfigValidationError
from app.manager import ConfigManager
from app.merge import MergeResult, merge_layers
from app.model import ConfigSnapshot
from app.sources import ConfigSource

logger = logging.getLogger("config.service")


@dataclass(frozen=True)
class LoadOutcome:
    """一次成功加载的结果。"""

    snapshot: ConfigSnapshot
    provenance: dict[str, str]


class ConfigService:
    """按优先级加载各来源 -> 合并 -> 校验 -> 原子发布。"""

    def __init__(self, manager: ConfigManager, sources: list[ConfigSource]) -> None:
        self._manager = manager
        self._sources = list(sources)

    def _read_layers(self) -> list[tuple[str, dict[str, Any]]]:
        layers: list[tuple[str, dict[str, Any]]] = []
        for source in self._sources:
            try:
                layers.append((source.name, source.load()))
            except ConfigSourceError:
                raise
            except Exception as exc:  # 来源实现抛出的非预期错误也要可区分
                raise ConfigSourceError(
                    f"CONFIG_SOURCE_UNEXPECTED: source={source.name}: {exc!r}"
                ) from exc
        return layers

    def load(self, *, initial: bool = False) -> LoadOutcome:
        """读取并合并所有来源，然后发布到管理器。

        initial=True：任何失败向上抛出（服务不应以错误配置启动）。
        initial=False：来源/校验失败统一包装为 ConfigUpdateRejected，
        管理器内上一个可用版本保持不变。
        """
        if initial:
            layers = self._read_layers()
            merged: MergeResult = merge_layers(layers)
            snapshot = self._manager.load_initial(merged.data)
            logger.info(
                "config initial-published version=%s sources=%s",
                snapshot.version,
                [name for name, _ in layers],
            )
            return LoadOutcome(snapshot=snapshot, provenance=merged.provenance)

        try:
            layers = self._read_layers()
            merged = merge_layers(layers)
            snapshot = self._manager.reload(merged.data)
        except (ConfigSourceError, ConfigValidationError) as exc:
            reasons: list[str]
            if isinstance(exc, ConfigSourceError):
                reasons = [f"SOURCE_ERROR: {exc}"]
            else:
                reasons = list(exc.reasons)
            current = self._manager.current()
            logger.warning(
                "config reload-rejected keep_version=%s reasons=%s", current.version, reasons
            )
            raise ConfigUpdateRejected(
                f"config update rejected; keeping version {current.version}", reasons
            ) from exc

        logger.info(
            "config published version=%s sources=%s provenance=%s",
            snapshot.version,
            [name for name, _ in layers],
            merged.provenance,
        )
        return LoadOutcome(snapshot=snapshot, provenance=merged.provenance)
