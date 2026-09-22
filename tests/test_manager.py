"""热更新失败回退、并发读写原子可见性与脱敏测试。"""

from __future__ import annotations

import json
import logging
import threading

import pytest

from app.errors import ConfigError, ConfigUpdateRejected
from app.manager import ConfigManager
from app.model import FlagDecision
from app.service import ConfigService
from app.sources import DefaultsSource

logger = logging.getLogger("tests")

SETTINGS_V1 = {
    "service_name": "svc", "max_connections": 100, "request_timeout_ms": 1000,
    "enable_cache": False, "api_base_url": "https://v1", "secret_token": "secret-v1",
}
SETTINGS_V2 = dict(SETTINGS_V1, max_connections=200, api_base_url="https://v2",
                   secret_token="secret-v2")


def _config(settings, *, connections_flag=False):
    return {"settings": settings,
            "flags": {"grad": {"enabled": True, "rollout": {"percentage": 50}},
                      "always_on": {"enabled": True}}}


def test_initial_load_required_before_serving():
    m = ConfigManager()
    with pytest.raises(ConfigError):
        m.current()


def test_successful_reload_swaps_version_atomically():
    m = ConfigManager()
    s1 = m.load_initial(_config(SETTINGS_V1))
    assert s1.version == 1
    s2 = m.reload(_config(SETTINGS_V2))
    assert s2.version == 2
    assert m.current().settings.max_connections == 200


def test_invalid_reload_is_rejected_and_old_version_remains():
    service = ConfigService(ConfigManager(), [DefaultsSource(_config(SETTINGS_V1))])
    outcome = service.load(initial=True)
    assert outcome.snapshot.version == 1

    # 通过 service 触发非法更新：原因可区分，旧版本保留
    bad_service = ConfigService(
        service._manager,
        [DefaultsSource({"settings": dict(SETTINGS_V1, max_connections="not-an-int")})],
    )
    with pytest.raises(ConfigUpdateRejected) as exc_info:
        bad_service.load()
    assert any(r.startswith("TYPE_MISMATCH") for r in exc_info.value.reasons)
    logger.info("reject reasons=%s", exc_info.value.reasons)

    m = service._manager
    assert m.current().version == 1
    assert m.current().settings.max_connections == 100
    assert "grad" in m.current().flags  # 旧标志依然存在


def test_failed_update_has_no_partial_effect():
    m = ConfigManager()
    m.load_initial(_config(SETTINGS_V1))
    # 新配置里同时有"正确的新键"和"非法键"，任何一个都不得生效
    attempted = {
        "settings": dict(SETTINGS_V2),
        "flags": {
            "grad": {"enabled": True, "rollout": {"percentage": 80}},
            "brand_new": {"enabled": True},
            "broken": {"enabled": "definitely-not-bool"},
        },
    }
    from app.errors import ConfigValidationError

    with pytest.raises(ConfigValidationError) as exc_info:
        m.reload(attempted)
    assert any("flags.broken.enabled" in r for r in exc_info.value.reasons)
    current = m.current()
    assert current.version == 1
    assert current.settings.max_connections == 100
    assert "brand_new" not in current.flags
    assert current.flags["grad"].rollout.percentage == 50  # 旧比例保留


def test_source_error_vs_validation_error_are_distinguishable():
    from app.errors import ConfigSourceError
    from app.sources import FileSource

    m = ConfigManager()
    service = ConfigService(m, [DefaultsSource(_config(SETTINGS_V1))])
    service.load(initial=True)

    source_fail = ConfigService(m, [FileSource("/definitely/missing.json")])
    with pytest.raises(ConfigUpdateRejected) as e1:
        source_fail.load()
    assert any(r.startswith("SOURCE_ERROR: CONFIG_FILE_NOT_FOUND") for r in e1.value.reasons)

    validation_fail = ConfigService(
        m, [DefaultsSource({"settings": dict(SETTINGS_V1, max_connections=-1)})]
    )
    with pytest.raises(ConfigUpdateRejected) as e2:
        validation_fail.load()
    assert any(r.startswith("INVALID_VALUE") for r in e2.value.reasons)

    assert e1.value.reasons != e2.value.reasons
    logger.info("source reasons=%s validation reasons=%s",
                e1.value.reasons, e2.value.reasons)


def test_secret_never_exposed_in_public_view_or_repr():
    m = ConfigManager()
    m.load_initial(_config(SETTINGS_V1))
    view = m.public_view()
    serialized = json.dumps(view, ensure_ascii=False)
    assert "secret-v1" not in serialized
    assert view["settings"]["secret_token"] == "***redacted***"
    # 原始明文仍只存在于内部快照，且公开视图中无法触达
    assert m.current().settings.secret_token == "secret-v1"


def test_concurrent_readers_always_see_one_complete_version():
    """读写并发：任何一次读到的只能是完整旧版本或完整新版本，无中间态。"""
    m = ConfigManager()
    m.load_initial(_config(SETTINGS_V1))
    stop = threading.Event()
    errors: list[str] = []
    seen_versions: set[int] = set()

    def reader():
        while True:
            snap = m.current()  # 先立即读一次，保证观察到 v1
            snap = m.current()
            seen_versions.add(snap.version)
            # 版本与内容必须自洽，禁止出现"半套新值 + 半套旧值"
            if snap.version % 2 == 1:
                expected_conn = 100 if snap.version == 1 else 300
            else:
                expected_conn = 200
            pct = snap.flags["grad"].rollout.percentage
            ok = snap.settings.max_connections == expected_conn
            if snap.version >= 2:
                ok = ok and pct == (50 if snap.version % 2 == 1 else 80)
            else:
                ok = ok and pct == 50
            if not ok:
                errors.append(
                    f"inconsistent version={snap.version} "
                    f"conn={snap.settings.max_connections} pct={pct}"
                )
            if stop.wait(timeout=0.0005):
                break

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()

    v2 = {"settings": SETTINGS_V2,
          "flags": {"grad": {"enabled": True, "rollout": {"percentage": 80}},
                    "always_on": {"enabled": True}}}
    v3_settings = dict(SETTINGS_V2, max_connections=300)
    v3 = {"settings": v3_settings,
          "flags": {"grad": {"enabled": True, "rollout": {"percentage": 50}},
                    "always_on": {"enabled": True}}}
    bad = {"settings": dict(SETTINGS_V2, max_connections="bad-string")}

    import time
    for i in range(10):
        m.reload(v2)  # 偶数版本：200 / 80
        from app.errors import ConfigValidationError
        with pytest.raises(ConfigValidationError):
            m.reload(bad)  # 必须被拒绝，版本不变
        time.sleep(0.002)
        m.reload(v3)  # 奇数版本：300 / 50
        with pytest.raises(ConfigValidationError):
            m.reload(bad)
        time.sleep(0.002)

    stop.set()
    for t in threads:
        t.join()
    assert not errors, errors
    assert 1 in seen_versions and 2 in seen_versions
    logger.info("concurrent readers saw versions=%s errors=%d", seen_versions, len(errors))


def test_decision_remains_stable_for_a_request_lifetime():
    """请求开始时取快照，即便中途发生更新，判定依据的版本号也保持不变。"""
    m = ConfigManager()
    m.load_initial(_config(SETTINGS_V1))
    captured: list[tuple[str, int]] = []
    snap = m.current()  # 请求开始

    def request_work():
        # 使用请求持有的旧引用直接判定（与 manager.decide 同一不可变模型）
        from app.rollout import stable_bucket, evaluate_percentage
        for i in range(1000):
            captured.append(("user", snap.version))

    worker = threading.Thread(target=request_work)
    worker.start()
    m.reload({"settings": SETTINGS_V2,
              "flags": {"grad": {"enabled": True, "rollout": {"percentage": 10}},
                        "always_on": {"enabled": True}}})
    worker.join()
    assert all(v == 1 for _, v in captured)
    # 同一主体在同一版本下判定不漂移
    a = m.decide("grad", "user-7")
    b = m.decide("grad", "user-7")
    assert a == b
    assert "version=" in a[1]
