"""热更新、失败保留旧版本、回滚与并发读写测试。"""

import threading

import pytest

from config_system.errors import ConfigRejectionError, RejectionReason
from config_system.manager import ConfigManager


def test_initial_load(schema, config_file):
    path = config_file({"fields": {"limit": 10, "api_key": "k1"}})
    mgr = ConfigManager(schema, file_path=path, environ={})
    snap = mgr.current()
    assert snap.get("limit") == 10
    assert snap.get("greeting") == "hello"  # 默认值层
    print(f"[test] initial version={snap.version}")


def test_hot_reload_success(schema, config_file):
    path = config_file({"fields": {"limit": 10, "api_key": "k1"}})
    mgr = ConfigManager(schema, file_path=path, environ={})
    v1 = mgr.current().version
    config_file({"fields": {"limit": 20, "api_key": "k2"}})
    snap = mgr.reload()
    assert snap.version != v1
    assert snap.get("limit") == 20
    print(f"[test] reload {v1} -> {snap.version}")


def test_hot_reload_failure_keeps_old_version(schema, config_file):
    path = config_file({"fields": {"limit": 10, "api_key": "k1"}})
    mgr = ConfigManager(schema, file_path=path, environ={})
    v1 = mgr.current().version
    # 写入非法配置：limit 越界
    config_file({"fields": {"limit": 99999, "api_key": "k2"}})
    with pytest.raises(ConfigRejectionError) as ei:
        mgr.reload()
    assert ei.value.reason is RejectionReason.ILLEGAL_VALUE
    # 旧版本继续服役，接口行为不变
    assert mgr.current().version == v1
    assert mgr.current().get("limit") == 10
    assert mgr.last_rejection is not None
    assert mgr.last_rejection.reason is RejectionReason.ILLEGAL_VALUE
    print(f"[test] rejected reload, reason={ei.value.reason.value}, "
          f"serving={mgr.current().version}")


def test_hot_reload_missing_required_keeps_old(schema, config_file):
    path = config_file({"fields": {"limit": 10, "api_key": "k1"}})
    mgr = ConfigManager(schema, file_path=path, environ={})
    v1 = mgr.current().version
    config_file({"fields": {}})  # 缺必填
    with pytest.raises(ConfigRejectionError) as ei:
        mgr.reload()
    assert ei.value.reason is RejectionReason.MISSING_REQUIRED
    assert mgr.current().version == v1


def test_env_overrides_file(schema, config_file):
    path = config_file({"fields": {"limit": 10, "api_key": "k1"}})
    mgr = ConfigManager(schema, file_path=path,
                        environ={"APP_FIELD_LIMIT": "77"})
    assert mgr.current().get("limit") == 77


def test_rollback(schema, config_file):
    path = config_file({"fields": {"limit": 10, "api_key": "k1"}})
    mgr = ConfigManager(schema, file_path=path, environ={})
    v1 = mgr.current().version
    config_file({"fields": {"limit": 20, "api_key": "k2"}})
    v2 = mgr.reload().version
    assert v2 != v1
    rolled = mgr.rollback()
    assert rolled.version == v1
    assert mgr.current().get("limit") == 10
    print(f"[test] rollback {v2} -> {rolled.version}")


def test_concurrent_read_write_atomic(schema, config_file):
    """并发读写：读者只能观察到完整的旧版本或新版本。"""
    path = config_file({"fields": {"limit": 10, "api_key": "k1"}})
    mgr = ConfigManager(schema, file_path=path, environ={})
    v1 = mgr.current().version
    config_file({"fields": {"limit": 20, "api_key": "k2"}})

    errors = []
    observed = set()
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            snap = mgr.current()
            observed.add(snap.version)
            # 版本与其内容必须自洽：不允许出现“新版本的版本号+旧版本的值”
            if snap.version == v1 and snap.get("limit") != 10:
                errors.append("v1 内容被污染")
            if snap.version != v1 and snap.get("limit") != 20:
                errors.append("v2 内容不完整")

    def writer():
        for _ in range(200):
            try:
                mgr.reload()
            except ConfigRejectionError:
                pass
        stop.set()

    threads = [threading.Thread(target=reader) for _ in range(8)]
    threads.append(threading.Thread(target=writer))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert observed <= {v1, mgr.current().version}
    assert mgr.current().version in observed
    print(f"[test] concurrent observed versions={sorted(observed)}")


def test_concurrent_mixed_valid_invalid_reloads(schema, config_file):
    """交替写入合法/非法配置，服务始终可用且版本完整。"""
    path = config_file({"fields": {"limit": 10, "api_key": "k1"}})
    mgr = ConfigManager(schema, file_path=path, environ={})
    stop = threading.Event()
    errors = []

    def reader():
        while not stop.is_set():
            snap = mgr.current()
            if snap.get("limit") not in (10, 30):
                errors.append(f"观察到部分生效: limit={snap.get('limit')}")

    def writer():
        for i in range(100):
            if i % 2 == 0:
                config_file({"fields": {"limit": 30, "api_key": "k3"}})
            else:
                config_file({"fields": {"limit": "bad", "api_key": "k3"}})
            try:
                mgr.reload()
            except ConfigRejectionError:
                pass
        stop.set()

    threads = [threading.Thread(target=reader) for _ in range(4)]
    threads.append(threading.Thread(target=writer))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert mgr.current().get("limit") == 30
