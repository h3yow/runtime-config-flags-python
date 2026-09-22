"""运行期配置管理器。

语义
----
* 读取：``current()`` 返回当前不可变快照，请求处理全程持有同一版本；
* 热更新：``reload()`` 重新读取 默认值->文件->环境变量 三层并校验，
  校验通过才原子替换快照；校验失败抛 ConfigRejectionError，
  当前版本原样保留，绝不部分生效；
* 回滚：``rollback()`` 恢复到上一个可用版本（若有）；
* 并发：快照替换与读取都在锁内完成，任一请求只能观察到完整的旧版本或新版本。
"""

from __future__ import annotations

import threading
from typing import Any

from .errors import ConfigRejectionError
from .model import ConfigSnapshot
from .schema import ConfigSchema
from .sources import defaults_layer, load_env_layer, load_file_layer
from .validator import DEFAULT_SALT, build_config, merge_layers


class ConfigManager:
    def __init__(
        self,
        schema: ConfigSchema,
        file_path: str | None = None,
        env_prefix: str = "APP_",
        salt: str = DEFAULT_SALT,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._schema = schema
        self._file_path = file_path
        self._env_prefix = env_prefix
        self._salt = salt
        self._environ = environ
        self._lock = threading.Lock()
        self._snapshot: ConfigSnapshot | None = None
        self._previous: ConfigSnapshot | None = None
        self._last_rejection: ConfigRejectionError | None = None
        # 启动时必须能构建出第一个可用版本，否则直接失败（无旧版本可回退）。
        self._snapshot = self._build_candidate()

    def _build_candidate(self) -> ConfigSnapshot:
        layers = [defaults_layer(self._schema)]
        if self._file_path is not None:
            layers.append(load_file_layer(self._file_path))
        layers.append(load_env_layer(self._env_prefix, self._environ))
        merged = merge_layers(*layers)
        return build_config(merged, self._schema, salt=self._salt)

    def current(self) -> ConfigSnapshot:
        with self._lock:
            assert self._snapshot is not None
            return self._snapshot

    @property
    def last_rejection(self) -> ConfigRejectionError | None:
        with self._lock:
            return self._last_rejection

    def reload(self) -> ConfigSnapshot:
        """热更新。成功返回新快照；失败抛 ConfigRejectionError 且保留旧版本。"""
        try:
            candidate = self._build_candidate()
        except ConfigRejectionError as exc:
            with self._lock:
                # 记录拒绝原因（附当前仍在服役的版本号），旧版本继续服役
                current_version = self._snapshot.version if self._snapshot else None
                exc.version = current_version
                self._last_rejection = exc
            raise
        with self._lock:
            assert self._snapshot is not None
            if candidate.version != self._snapshot.version:
                self._previous = self._snapshot
                self._snapshot = candidate
            self._last_rejection = None
            return self._snapshot

    def rollback(self) -> ConfigSnapshot:
        """回滚到上一个可用版本；没有历史版本时返回当前版本。"""
        with self._lock:
            assert self._snapshot is not None
            if self._previous is not None:
                self._snapshot, self._previous = self._previous, self._snapshot
            return self._snapshot

    def status(self) -> dict[str, Any]:
        with self._lock:
            assert self._snapshot is not None
            return {
                "version": self._snapshot.version,
                "previous_version": self._previous.version if self._previous else None,
                "last_rejection": (
                    None if self._last_rejection is None else {
                        "reason": self._last_rejection.reason.value,
                        "path": self._last_rejection.path,
                        "detail": self._last_rejection.detail,
                        "serving_version": self._last_rejection.version,
                    }
                ),
            }
