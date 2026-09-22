"""端到端 HTTP 测试：既有接口不变、版本与依据可观察、拒绝原因可区分、并发判定。"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import router
from app.manager import ConfigManager
from app.service import ConfigService
from app.sources import DefaultsSource

logger = logging.getLogger("tests")

V1 = {
    "settings": {
        "service_name": "svc", "max_connections": 100, "request_timeout_ms": 1000,
        "enable_cache": True, "api_base_url": "https://v1", "secret_token": "shh-v1",
    },
    "flags": {
        "new_ui": {"enabled": True, "rollout": {"percentage": 20}},
        "killswitch": {"enabled": False},
    },
}
V2 = {
    "settings": dict(V1["settings"], max_connections=300, secret_token="shh-v2"),
    "flags": {
        "new_ui": {"enabled": True, "rollout": {"percentage": 80}},
        "killswitch": {"enabled": True},
        "new_flag": {"enabled": True},
    },
}
BAD_TYPE = {"settings": dict(V1["settings"], max_connections="oops")}
BAD_VALUE = {"settings": dict(V1["settings"], max_connections=999999)}
BAD_REQUIRED = {"settings": {k: v for k, v in V1["settings"].items()
                             if k != "secret_token"}}


def _make_app(initial_raw=None):
    # 每个测试用独立的 manager/service，并通过依赖覆盖接入路由
    manager = ConfigManager()
    initial_raw = V1 if initial_raw is None else initial_raw
    manager.load_initial(initial_raw)
    service = ConfigService(manager, [DefaultsSource(V2)])
    app = FastAPI()
    app.include_router(router)
    app.state.config_manager = manager
    app.state.config_service = service
    return app, manager, service


def test_existing_root_endpoint_unchanged():
    os.environ.setdefault("APP_CONFIG_FILE", "config.example.json")
    from main import app

    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert r.json() == {"Hello": "World"}


def test_config_endpoint_shows_version_and_redacts_secret():
    app, _, _ = _make_app()
    with TestClient(app) as client:
        r = client.get("/config")
        assert r.status_code == 200
        body = r.json()
        assert body["version"] == 1
        assert body["settings"]["secret_token"] == "***redacted***"
        assert "shh-v1" not in r.text
        logger.info("config view version=%s flags=%s", body["version"], body["flags"])


def test_flag_evaluation_reports_version_and_reason():
    app, _, _ = _make_app()
    with TestClient(app) as client:
        # 灰度开关：响应里包含版本、桶号、阈值等可解释依据
        r = client.get("/flags/new_ui", params={"subject": "alice"})
        assert r.status_code == 200
        body = r.json()
        assert body["version"] == 1
        assert "bucket=" in body["reason"] and "threshold=" in body["reason"]
        assert "version=1" in body["reason"]
        logger.info("decision: %s", body["reason"])

        # 关闭的开关
        r = client.get("/flags/killswitch", params={"subject": "alice"})
        assert r.json()["decision"] == "off"
        assert "flag_disabled" in r.json()["reason"]

        # 未知开关
        r = client.get("/flags/nope")
        assert r.json()["decision"] == "off"
        assert "flag_unknown" in r.json()["reason"]


def test_grouping_is_stable_via_http_for_same_version():
    app, manager, _ = _make_app()
    decisions = {}
    with TestClient(app) as client:
        first = client.get("/flags/new_ui", params={"subject": "alice"}).json()
        for _ in range(20):
            again = client.get("/flags/new_ui", params={"subject": "alice"}).json()
            assert again["decision"] == first["decision"]
            assert again["reason"] == first["reason"]  # 桶号也完全一致
        decisions["alice"] = first
        assert decisions["alice"]["version"] == 1
        logger.info("stable http decision: %s", first["reason"])


def test_successful_reload_publishes_new_version():
    app, _, service = _make_app()
    with TestClient(app) as client:
        r = client.post("/admin/reload")
        assert r.status_code == 200
        assert r.json() == {"status": "published", "version": 2}
        body = client.get("/config").json()
        assert body["version"] == 2
        assert body["settings"]["max_connections"] == 300
        assert "shh-v2" not in client.get("/config").text


def test_reload_with_invalid_config_returns_distinguishable_422():
    app, manager, _ = _make_app()
    # 换成始终返回坏配置（三种不同原因）的 service
    for bad, marker in [
        (BAD_TYPE, "TYPE_MISMATCH"),
        (BAD_VALUE, "INVALID_VALUE"),
        (BAD_REQUIRED, "MISSING_REQUIRED"),
    ]:
        bad_service = ConfigService(manager, [DefaultsSource(bad)])
        app.state.config_service = bad_service
        with TestClient(app) as client:
            r = client.post("/admin/reload")
            assert r.status_code == 422
            detail = r.json()["detail"]
            assert detail["error"] == "config_update_rejected"
            assert any(marker in reason for reason in detail["reasons"]), detail
            logger.warning("rejected version stays at=%s reasons=%s",
                           client.get("/config").json()["version"], detail["reasons"])
        # 拒绝后服务仍正常，版本仍是 1
        with TestClient(app) as client:
            assert client.get("/config").json()["version"] == 1
            assert client.get("/").status_code == 404  # 本测试 app 未挂根路由
    # 恢复合法 service，可正常升到 v2
    good_service = ConfigService(manager, [DefaultsSource(V2)])
    app.state.config_service = good_service
    with TestClient(app) as client:
        assert client.post("/admin/reload").json()["version"] == 2


def test_service_stays_available_after_repeated_failed_updates():
    app, manager, _ = _make_app()
    bad_service = ConfigService(manager, [DefaultsSource(BAD_TYPE)])
    app.state.config_service = bad_service
    with TestClient(app) as client:
        for _ in range(5):
            assert client.post("/admin/reload").status_code == 422
        # 接口行为与版本完全不变，服务可用
        assert client.get("/config").json()["version"] == 1
        assert client.get("/flags/killswitch").json()["decision"] == "off"


def test_concurrent_requests_see_consistent_versions_during_reloads():
    app, manager, service = _make_app()
    stop = threading.Event()
    decisions: dict[int, set[str]] = {}
    errors: list[Exception] = []

    def worker(client: TestClient):
        # 忙跑有限次数即可，避免纯忙等导致测试无法结束
        for _ in range(300):
            if stop.is_set():
                break
            try:
                r = client.get("/flags/new_ui", params={"subject": "alice"})
                body = r.json()
                # 同一版本下 alice 的判定依据必须唯一且自洽
                decisions.setdefault(body["version"], set()).add(body["reason"])
            except Exception as exc:  # noqa: BLE001 - 并发期任何异常都记录
                errors.append(exc)

    with TestClient(app) as client:
        threads = [threading.Thread(target=worker, args=(client,)) for _ in range(4)]
        for t in threads:
            t.start()
        good = ConfigService(manager, [DefaultsSource(V2)])
        bad = ConfigService(manager, [DefaultsSource(BAD_TYPE)])
        for i in range(10):
            # 成功发布新版本
            app.state.config_service = good
            manager.reload(V2)
            time.sleep(0.001)
            # 非法更新走完整拒绝路径，版本保持不变
            app.state.config_service = bad
            try:
                bad.load()
            except Exception:
                pass
            manager.reload(V1)
            app.state.config_service = good
            time.sleep(0.001)
        stop.set()
        for t in threads:
            t.join()

    assert not errors, errors
    assert decisions, "no requests were observed during reloads"
    for version, reasons in decisions.items():
        # 同一版本下所有并发请求对同一主体的判定依据必须完全一致
        assert len(reasons) == 1, (version, reasons)
        logger.info("version=%s consistent reason=%s", version, next(iter(reasons)))


def test_decision_log_contains_version_and_basis(caplog):
    """测试日志中必须打印配置版本与判定依据。"""
    app, _, _ = _make_app()
    with TestClient(app) as client:
        with caplog.at_level(logging.INFO, logger="config.api"):
            r = client.get("/flags/new_ui", params={"subject": "alice"})
    assert r.status_code == 200
    version = r.json()["version"]
    records = [m for m in caplog.messages if "flag decision:" in m]
    assert records, caplog.messages
    msg = records[-1]
    assert f"version={version}" in msg
    assert "bucket=" in msg and "percentage=" in msg
