"""可区分的配置拒绝原因。"""

from __future__ import annotations

import enum


class RejectionReason(enum.Enum):
    TYPE_MISMATCH = "type_mismatch"
    ILLEGAL_VALUE = "illegal_value"
    MISSING_REQUIRED = "missing_required"
    DUPLICATE_KEY = "duplicate_key"
    SOURCE_READ_ERROR = "source_read_error"
    INVALID_KEY = "invalid_key"


class ConfigRejectionError(Exception):
    """配置被拒绝时抛出，携带可区分原因、字段路径与版本信息。"""

    def __init__(self, reason: RejectionReason, path: str, detail: str, version: str | None = None) -> None:
        self.reason = reason
        self.path = path
        self.detail = detail
        self.version = version
        super().__init__(f"[{reason.value}] {path}: {detail}")
