"""批量判定测试：整批同版本、空/重复输入、并发热更新不串版本、规模。"""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from config_system.errors import ConfigRejectionError
from config_system.manager import ConfigManager
from config_system.service import create_app
from config_system.schema import ConfigSchema, FieldSpec, FlagSpec


@pytest.fixture
def schema():
    return ConfigSchema(
        fields=(FieldSpec(key="limit", type="int", default=10),),
        flags=(FlagSpec(name="new_ui"),),
    )


def _write(path, data):
    with open(path, "w") as fh:
        json.dump(data, fh)


@pytest.fixture
def setup(schema, tmp_path):
    path = str(tmp_path / "config.json")
    _write(path, {"fields": {"limit": 10},
                  "flags": {"new_ui": {"enabled": True, "percentage": 50,
                                       "force_on": ["vip-1"],
                                       "force_off": ["bad-1"]}}})
    mgr = ConfigManager(schema, file_path=path, environ={})
    return TestClient(create_app(mgr)), mgr, path


def test_batch_all_same_version_with_reason_per_subject(setup):
    client, mgr, _ = setup
    subjects = [f"user-{i}" for i in range(100)] + ["vip-1", "bad-1"]
    body = client.post("/flags/new_ui/batch", json={"subjects": subjects}).json()
    assert body["version"] == mgr.current().version
    assert body["count"] == len(subjects)
    assert len(body["results"]) == len(subjects)
    # 整批同一版本；每个结果都带版本号与判定依据
    versions = {r["version"] for r in body["results"]}
    assert versions == {body["version"]}
    reasons = {r["reason"] for r in body["results"]}
    assert reasons <= {"rollout_in", "rollout_out", "force_on", "force_off", "global_off"}
    by_subject = {r["subject"]: r for r in body["results"]}
    assert by_subject["vip-1"]["reason"] == "force_on" and by_subject["vip-1"]["enabled"] is True
    assert by_subject["bad-1"]["reason"] == "force_off" and by_subject["bad-1"]["enabled"] is False
    print(f"[test] batch n={body['count']} version={body['version']} reasons="
          f"{sorted(reasons)}")


def test_batch_empty(setup):
    client, mgr, _ = setup
    body = client.post("/flags/new_ui/batch", json={"subjects": []}).json()
    assert body["results"] == []
    assert body["count"] == 0
    # 空批次仍返回当前版本号，调用方可确认基准版本
    assert body["version"] == mgr.current().version
    print(f"[test] empty batch version={body['version']} results=[]")


def test_batch_duplicate_subjects_consistent(setup):
    client, _, _ = setup
    seq = ["dup", "user-1", "dup", "", "dup"]
    body = client.post("/flags/new_ui/batch", json={"subjects": seq}).json()
    assert [r["subject"] for r in body["results"]] == seq  # 顺序与逐位保留
    dup_results = [r for r in body["results"] if r["subject"] == "dup"]
    assert all(r["enabled"] == dup_results[0]["enabled"] for r in dup_results)
    assert all(r["reason"] == dup_results[0]["reason"] for r in dup_results)
    print(f"[test] duplicates stable: reason={dup_results[0]['reason']} "
          f"enabled={dup_results[0]['enabled']}")


def test_batch_bad_shapes_422(setup):
    client, _, _ = setup
    assert client.post("/flags/new_ui/batch", json={"subjects": "u1"}).status_code == 422
    assert client.post("/flags/new_ui/batch", json={"subjects": [1, 2]}).status_code == 422
    assert client.post("/flags/new_ui/batch", json={}).status_code == 422


def test_batch_unknown_flag(setup):
    client, _, _ = setup
    body = client.post("/flags/nope/batch", json={"subjects": ["a", "b"]}).json()
    assert body["count"] == 2
    assert all(r["reason"] == "unknown_flag" and r["enabled"] is False
               for r in body["results"])
    assert all(r["version"] == body["version"] for r in body["results"])


def test_batch_during_concurrent_reload_no_version_mixing(setup, schema):
    """一边批量判定一边热更新：每一批必须整批同一版本，不允许串版本。"""
    client, mgr, path = setup
    subjects = [f"user-{i}" for i in range(500)]
    stop = threading.Event()
    violations = []
    batch_versions = set()

    def reader():
        while not stop.is_set():
            body = client.post("/flags/new_ui/batch", json={"subjects": subjects}).json()
            top = body["version"]
            batch_versions.add(top)
            for idx, r in enumerate(body["results"]):
                if r["version"] != top:
                    violations.append(f"batch mixed at idx={idx}: "
                                      f"{r['version']} != {top}")
                    break

    def writer():
        for i in range(120):
            _write(path, {"fields": {"limit": 10},
                          "flags": {"new_ui": {"enabled": True,
                                               "percentage": 10 + (i % 8) * 10}}})
            try:
                mgr.reload()
            except ConfigRejectionError:
                pass
            # 夹杂一次非法更新（比例越界），被拒后旧版本继续服役
            _write(path, {"fields": {"limit": 10},
                          "flags": {"new_ui": {"enabled": True, "percentage": 999}}})
            try:
                mgr.reload()
            except ConfigRejectionError:
                pass
        stop.set()

    threads = [threading.Thread(target=reader) for _ in range(6)]
    threads.append(threading.Thread(target=writer))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not violations
    assert len(batch_versions) >= 2  # 确实观察到了多个版本
    print(f"[test] concurrent batch: observed_versions={sorted(batch_versions)} "
          f"mixing_violations=0")


def test_batch_scale_thousands_subjects(setup):
    """几千主体的批量判定应在亚秒级完成（纯 O(n) 哈希，无退化）。"""
    client, _, _ = setup

    def time_batch(n):
        subjects = [f"user-{i}" for i in range(n)]
        t0 = time.perf_counter()
        body = client.post("/flags/new_ui/batch", json={"subjects": subjects}).json()
        return time.perf_counter() - t0, body["version"]

    t_1k, v1 = time_batch(1000)
    t_4k, v2 = time_batch(4000)
    assert v1 == v2
    # 4k 应在 1s 内；且每主体均摊不随批量显著变差（4x 数据量耗时 < 8x）
    assert t_4k < 1.0
    assert t_4k < t_1k * 8 + 0.2
    print(f"[test] batch scale: 1000 in {t_1k*1000:.1f}ms, "
          f"4000 in {t_4k*1000:.1f}ms version={v1}")
