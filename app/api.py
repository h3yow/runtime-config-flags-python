"""HTTP 接口层（不改变既有 GET / 行为）。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from app.bootstrap import get_config_manager, get_config_service
from app.errors import ConfigError, ConfigUpdateRejected
from app.manager import ConfigManager
from app.service import ConfigService

logger = logging.getLogger("config.api")

router = APIRouter()


def _manager(request: Request) -> ConfigManager:
    # 优先用 app.state 注入（测试隔离），否则回退到进程级单例
    manager = getattr(request.app.state, "config_manager", None)
    return manager if manager is not None else get_config_manager()


def _service(request: Request) -> ConfigService:
    service = getattr(request.app.state, "config_service", None)
    return service if service is not None else get_config_service()


@router.get("/config")
def get_config(request: Request):
    """查看当前配置版本（密钥脱敏，绝不返回明文）。"""
    try:
        return _manager(request).public_view()
    except ConfigError as exc:
        raise HTTPException(status_code=503, detail={"error": str(exc)}) from exc


@router.post("/admin/reload")
def reload_config(request: Request):
    """触发一次热更新。

    成功：返回新版本号；失败：HTTP 422 并给出可区分的拒绝原因，
    响应中同时返回继续沿用的旧版本号。
    """
    try:
        outcome = _service(request).load(initial=False)
    except ConfigUpdateRejected as exc:
        logger.warning("reload rejected: %s", exc.reasons)
        kept_version = _manager(request).current().version
        raise HTTPException(
            status_code=422,
            detail={
                "error": "config_update_rejected",
                "kept_version": kept_version,
                "message": str(exc),
                "reasons": exc.reasons,
            },
        ) from exc
    return {"status": "published", "version": outcome.snapshot.version}


@router.get("/flags/{name}")
def evaluate_flag(
    request: Request,
    name: str,
    subject: str | None = Query(default=None, description="灰度分组主体标识"),
):
    """判定特性开关，响应与日志均带配置版本与判定依据。"""
    manager = _manager(request)
    try:
        # 只取一次快照，保证版本号与判定依据来自同一个完整版本
        snapshot = manager.current()
        decision, reason = manager.decide_snapshot(snapshot, name, subject)
    except ConfigError as exc:
        raise HTTPException(status_code=503, detail={"error": str(exc)}) from exc
    logger.info("flag decision: %s", reason)
    return {
        "flag": name,
        "subject": subject,
        "decision": decision.value,
        "version": snapshot.version,
        "reason": reason,
    }
