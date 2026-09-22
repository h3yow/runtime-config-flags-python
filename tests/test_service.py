"""HTTP 层测试：既有接口不变、热更新失败可区分、密钥脱敏、判定可解释。"""

import json

from fastapi.testclient import TestClient

from config_system.manager import ConfigManager
from config_system.service import create_app
from main import app as main_app


def _client(schema, config_file, data, **kw):
    path = config_file(data)
    mgr = ConfigManager(schema, file_path=path, environ={}, **kw)
    return TestClient(create_app(mgr)), mgr, config_file


def test_existing_root_unchanged():
    client = TestClient(main_app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"Hello": "World"}


def test_config_endpoint_redacts_secret(schema, config_file):
    client, _, _ = _client(schema, config_file,
                           {"fields": {"limit": 10, "api_key": "plain-text-secret"}})
    resp = client.get("/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fields"]["api_key"] == "***REDACTED***"
    assert "plain-text-secret" not in json.dumps(body)
    print(f"[test] /config version={body['version']} (secret redacted)")


def test_reload_success_and_failure_distinguishable(schema, config_file):
    client, mgr, write = _client(schema, config_file,
                                 {"fields": {"limit": 10, "api_key": "k1"}})
    v1 = client.get("/config").json()["version"]

    # 成功热更新
    write({"fields": {"limit": 20, "api_key": "k2"}})
    resp = client.post("/config/reload")
    assert resp.status_code == 200
    v2 = resp.json()["version"]
    assert v2 != v1
    assert client.get("/config").json()["fields"]["limit"] == 20

    # 失败热更新：类型不符
    write({"fields": {"limit": "oops", "api_key": "k3"}})
    resp = client.post("/config/reload")
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["reason"] == "type_mismatch"
    assert err["serving_version"] == v2
    # 服务仍用旧版本
    assert client.get("/config").json()["version"] == v2

    # 失败热更新：缺必填（原因可区分）
    write({"fields": {"greeting": "x"}})
    resp = client.post("/config/reload")
    assert resp.status_code == 422
    assert resp.json()["error"]["reason"] == "missing_required"
    assert client.get("/config").json()["version"] == v2
    print(f"[test] reload v1={v1} v2={v2}; rejections distinguished, serving={v2}")


def test_rollback_endpoint(schema, config_file):
    client, mgr, write = _client(schema, config_file,
                                 {"fields": {"limit": 10, "api_key": "k1"}})
    v1 = client.get("/config").json()["version"]
    write({"fields": {"limit": 20, "api_key": "k2"}})
    client.post("/config/reload")
    resp = client.post("/config/rollback")
    assert resp.status_code == 200
    assert resp.json()["version"] == v1
    assert client.get("/config").json()["fields"]["limit"] == 10


def test_flag_decision_explains_basis(schema, config_file):
    client, mgr, write = _client(schema, config_file, {
        "fields": {"limit": 10, "api_key": "k1"},
        "flags": {"beta": {"enabled": True},
                  "new_ui": {"enabled": True, "percentage": 50}},
    })
    resp = client.get("/flags/beta")
    body = resp.json()
    assert body["enabled"] is True
    assert body["reason"] == "boolean_on"
    print(f"[test] flag beta: version={body['version']} reason={body['reason']}")

    resp = client.get("/flags/new_ui", params={"subject": "user-1"})
    body = resp.json()
    assert body["reason"] in ("rollout_in", "rollout_out")
    assert 0 <= body["bucket"] <= 99
    # 同主体同版本判定稳定
    again = client.get("/flags/new_ui", params={"subject": "user-1"}).json()
    assert again["enabled"] == body["enabled"]
    assert again["bucket"] == body["bucket"]
    print(f"[test] flag new_ui: version={body['version']} bucket={body['bucket']} "
          f"percentage={body['percentage']} reason={body['reason']}")

    resp = client.get("/flags/nonexistent")
    assert resp.json()["reason"] == "unknown_flag"
    assert resp.json()["enabled"] is False


def test_rollout_grouping_stable_across_unrelated_reload(schema, config_file):
    """无关字段变更不应导致灰度分组漂移（盐与版本解耦）。"""
    client, mgr, write = _client(schema, config_file, {
        "fields": {"limit": 10, "api_key": "k1"},
        "flags": {"new_ui": {"enabled": True, "percentage": 40}},
    })
    subjects = [f"user-{i}" for i in range(50)]
    before = {s: client.get("/flags/new_ui", params={"subject": s}).json()["enabled"]
              for s in subjects}
    # 修改无关字段并热更新（版本变化，但灰度比例不变）
    write({"fields": {"limit": 99, "api_key": "k1"},
           "flags": {"new_ui": {"enabled": True, "percentage": 40}}})
    client.post("/config/reload")
    after = {s: client.get("/flags/new_ui", params={"subject": s}).json()["enabled"]
             for s in subjects}
    assert before == after
    drift = sum(1 for s in subjects if before[s] != after[s])
    print(f"[test] grouping drift after unrelated reload: {drift}/{len(subjects)}")
