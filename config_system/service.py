"""FastAPI 装配：在既有服务上增加配置/开关接口，不改变既有路由行为。"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.responses import JSONResponse

from .errors import ConfigRejectionError
from .manager import ConfigManager
from .schema import ConfigSchema, FieldSpec, FlagSpec

logger = logging.getLogger("config_system")

# 演示服务的配置规格：全部带默认值，保证无配置文件也能启动。
APP_SCHEMA = ConfigSchema(
    fields=(
        FieldSpec(key="greeting", type="str", default="Hello"),
        FieldSpec(key="limit", type="int", default=100, min_value=1, max_value=100000),
        FieldSpec(key="api_key", type="secret", default=""),
    ),
    flags=(
        FlagSpec(name="beta"),
        FlagSpec(name="new_ui"),
    ),
)


def build_default_manager() -> ConfigManager:
    file_path = os.environ.get("APP_CONFIG_FILE", "config.json")
    return ConfigManager(APP_SCHEMA, file_path=file_path)


class BatchFlagRequest(BaseModel):
    # 显式要求 list[str]：标量等错误形状由 pydantic 统一拒绝（422）
    subjects: list[str]


def create_app(manager: ConfigManager | None = None) -> FastAPI:
    app = FastAPI(title="runtime-config-flags")
    mgr = manager if manager is not None else build_default_manager()
    app.state.config_manager = mgr

    @app.get("/config")
    def get_config():
        """当前生效配置（密钥已脱敏）。"""
        return mgr.current().public_view()

    @app.get("/config/status")
    def get_status():
        return mgr.status()

    @app.post("/config/reload")
    def reload_config():
        """热更新：失败返回 422 与可区分原因，旧版本继续服役。"""
        try:
            snap = mgr.reload()
        except ConfigRejectionError as exc:
            logger.warning("config reload rejected: %s", exc)
            return JSONResponse(status_code=422, content={
                "error": {
                    "reason": exc.reason.value,
                    "path": exc.path,
                    "detail": exc.detail,
                    "serving_version": exc.version,
                }
            })
        logger.info("config reloaded: version=%s", snap.version)
        return {"version": snap.version, "status": mgr.status()}

    @app.post("/config/rollback")
    def rollback_config():
        snap = mgr.rollback()
        logger.info("config rollback: version=%s", snap.version)
        return {"version": snap.version, "status": mgr.status()}

    @app.get("/flags/{name}")
    def eval_flag(name: str, subject: str | None = None):
        """开关判定：返回配置版本与判定依据。"""
        snap = mgr.current()
        decision = snap.decide(name, subject)
        logger.info("flag decide: version=%s flag=%s subject=%s -> %s (%s)",
                    decision["version"], name, subject,
                    decision["enabled"], decision["reason"])
        return decision

    @app.post("/flags/{name}/batch")
    def eval_flag_batch(name: str, req: BatchFlagRequest):
        """批量判定：整批一次取快照，结果全部基于同一配置版本。

        空批次返回空 results；重复主体逐个返回（结果一致），顺序与请求一致。
        """
        snap = mgr.current()
        result = snap.decide_batch(name, req.subjects)
        reasons: dict[str, int] = {}
        for item in result["results"]:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        logger.info("flag batch decide: version=%s flag=%s count=%s reasons=%s",
                    result["version"], name, result["count"], reasons)
        return result

    return app
