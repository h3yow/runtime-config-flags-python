"""强制名单 / 一键全量关 / 批量判定的新行为测试。

所有用例均打印配置版本号与判定依据（reason），方便出问题时对照日志。
"""

import json
import threading

import pytest
from fastapi.testclient import TestClient

from config_system.errors import ConfigRejectionError, RejectionReason
from config_system.manager import ConfigManager
from config_system.rollout import stable_bucket
from config_system.schema import ConfigSchema, FieldSpec, FlagSpec
from config_system.service import create_app


@pytest.fixture
def schema():
    return ConfigSchema(
        fields=(FieldSpec(key="greeting", type="str", default="hi"),),
        flags=(FlagSpec(name="new_ui"), FlagSpec(name="beta")),
    )


def _manager(schema, tmp_path, flag_cfg):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"flags": flag_cfg}))
    return ConfigManager(schema, file_path=str(path), environ={}), path


def _find_subject(flag, salt, percentage, *, inside):
    """找一个桶位落在灰度内/外的主体。"""
    for i in range(100000):
        s = f"user-{i}"
        b = stable_bucket(flag, s, salt)
        if inside:
            if b < percentage:
                return s
        elif b >= percentage:
            return s
    raise AssertionError("未找到符合条件的主体")


# ---------- 整体关闭（kill_switch）与比例叠加 ----------

def test_kill_switch_overrides_everything(schema, tmp_path):
    """整体关闭优先级最高：100% 灰度 + 强制打开名单命中，也一律关闭。"""
    mgr, _ = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 100,
                   "force_on": ["vip-1"], "kill_switch": True},
    })
    snap = mgr.current()
    for subject in ["vip-1", "any-user", ""]:
        d = snap.decide("new_ui", subject)
        assert d["enabled"] is False
        assert d["reason"] == "kill_switch"
    print(f"[test] kill_switch@100% version={snap.version} -> all kill_switch")


def test_kill_switch_at_zero_percentage_is_determinate(schema, tmp_path):
    """比例 0 叠加整体关闭：reason 必须是 kill_switch 而不是 rollout_out。"""
    mgr, _ = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 0, "kill_switch": True},
    })
    d = mgr.current().decide("new_ui", "user-1")
    assert d["enabled"] is False and d["reason"] == "kill_switch"
    print(f"[test] kill_switch@0% version={mgr.current().version} reason={d['reason']}")


def test_zero_and_hundred_percentage_without_kill(schema, tmp_path):
    """边界比例：0 全关（rollout_out）、100 全开（rollout_in）。"""
    mgr0, _ = _manager(schema, tmp_path, {"new_ui": {"enabled": True, "percentage": 0}})
    d0 = mgr0.current().decide("new_ui", "user-1")
    assert d0["enabled"] is False and d0["reason"] == "rollout_out"
    mgr100, _ = _manager(schema, tmp_path, {"new_ui": {"enabled": True, "percentage": 100}})
    d100 = mgr100.current().decide("new_ui", "user-1")
    assert d100["enabled"] is True and d100["reason"] == "rollout_in"
    print(f"[test] boundary 0%/100%: {d0['reason']}@{d0['version']} / {d100['reason']}@{d100['version']}")


def test_kill_switch_off_restores_normal_rollout(schema, tmp_path):
    """解除整体关闭后恢复比例判定。"""
    mgr, path = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 100, "kill_switch": True}})
    assert mgr.current().decide("new_ui", "u")["reason"] == "kill_switch"
    path.write_text(json.dumps({"flags": {
        "new_ui": {"enabled": True, "percentage": 100, "kill_switch": False}}}))
    snap = mgr.reload()
    d = snap.decide("new_ui", "u")
    assert d["enabled"] is True and d["reason"] == "rollout_in"
    print(f"[test] kill released -> version={snap.version} reason={d['reason']}")


# ---------- 强制名单与比例叠加 ----------

def test_force_lists_ignore_percentage(schema, tmp_path):
    """名单命中与比例无关：force_on 在 0% 下仍开，force_off 在 100% 下仍关。"""
    mgr, _ = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 0, "force_on": ["vip-1"]},
    })
    d = mgr.current().decide("new_ui", "vip-1")
    assert d["enabled"] is True and d["reason"] == "force_on"
    d_other = mgr.current().decide("new_ui", "ordinary-1")
    assert d_other["enabled"] is False and d_other["reason"] == "rollout_out"

    mgr2, _ = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 100, "force_off": ["blocked-1"]},
    })
    d2 = mgr2.current().decide("new_ui", "blocked-1")
    assert d2["enabled"] is False and d2["reason"] == "force_off"
    d2_other = mgr2.current().decide("new_ui", "ordinary-2")
    assert d2_other["enabled"] is True and d2_other["reason"] == "rollout_in"
    print(f"[test] force lists stable: force_on@0% version={d['version']}, "
          f"force_off@100% version={d2['version']}")


def test_both_lists_close_wins(schema, tmp_path):
    """同一主体同时在两个名单：以关闭为准，结果稳定不摇摆。"""
    mgr, _ = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 50,
                   "force_on": ["conflict-1"], "force_off": ["conflict-1"]},
    })
    snap = mgr.current()
    reasons = {snap.decide("new_ui", "conflict-1")["reason"] for _ in range(10)}
    assert reasons == {"force_off"}
    assert snap.decide("new_ui", "conflict-1")["enabled"] is False
    print(f"[test] both-lists conflict version={snap.version} -> force_off (close wins)")


def test_force_off_beats_force_on_regardless_of_bucket(schema, tmp_path):
    """两名单命中时即使按比例本该开启，也强制关闭。"""
    salt = "runtime-config-flags"
    pct = 50
    inside = _find_subject("new_ui", salt, pct, inside=True)
    mgr, _ = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": pct,
                   "force_on": [inside], "force_off": [inside]},
    })
    d = mgr.current().decide("new_ui", inside)
    assert d["enabled"] is False and d["reason"] == "force_off"
    print(f"[test] close-wins subject={inside} bucket={stable_bucket('new_ui', inside, salt)} "
          f"version={d['version']}")


def test_list_members_not_disturbed_by_percentage_change(schema, tmp_path):
    """调比例不搅动分组：名单主体恒定命中；名单外主体只能进不能出。"""
    mgr, path = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 10,
                   "force_on": ["vip-1"], "force_off": ["blocked-1"]},
    })
    v1 = mgr.current()
    subjects = [f"user-{i}" for i in range(2000)] + ["vip-1", "blocked-1"]
    before = {s: v1.decide("new_ui", s) for s in subjects}

    # 比例 10 -> 60，名单不变
    path.write_text(json.dumps({"flags": {
        "new_ui": {"enabled": True, "percentage": 60,
                   "force_on": ["vip-1"], "force_off": ["blocked-1"]}}}))
    v2 = mgr.reload()
    assert v2.version != v1.version

    kicked = []
    for s, old in before.items():
        new = v2.decide("new_ui", s)
        if old["enabled"] and not new["enabled"]:
            kicked.append(s)
    assert kicked == [], f"以下主体在调大比例后被踢出: {kicked[:5]}"
    # 名单主体理由不变，与比例完全无关
    assert v2.decide("new_ui", "vip-1")["reason"] == "force_on"
    assert v2.decide("new_ui", "blocked-1")["reason"] == "force_off"
    # 比例扩张确实放进来了一批名单外主体
    newcomers = [s for s in subjects if not before[s]["enabled"]
                 and v2.decide("new_ui", s)["enabled"]
                 and v2.decide("new_ui", s)["reason"] == "rollout_in"]
    assert newcomers
    print(f"[test] percentage 10->60: kicked=0, newcomers={len(newcomers)} "
          f"versions={v1.version}->{v2.version}")


def test_bad_lists_reject_entire_update(schema, tmp_path):
    """名单写坏（元素类型不对/名单本身不是数组）整次更新拒绝，旧版本继续服役。"""
    mgr, path = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 50, "force_on": ["vip-1"]},
    })
    good_version = mgr.current().version
    for bad_lists in (
        {"force_on": ["ok", 123]},
        {"force_off": "not-a-list"},
        {"force_on": [True]},
        {"force_on": ["ok"], "force_off": [{"id": "x"}]},
        {"kill_switch": "true"},
    ):
        path.write_text(json.dumps({"flags": {"new_ui": {
            "enabled": True, "percentage": 50, **bad_lists}}}))
        with pytest.raises(ConfigRejectionError) as ei:
            mgr.reload()
        assert ei.value.reason is RejectionReason.TYPE_MISMATCH
        # 整次更新失败：继续用上一个好版本，名单仍然有效
        assert mgr.current().version == good_version
        assert mgr.current().decide("new_ui", "vip-1")["reason"] == "force_on"
    print(f"[test] malformed lists all rejected, serving={mgr.current().version}")


def test_undeclared_flag_with_lists_rejected(schema, tmp_path):
    """给没声明的开关写名单：invalid_key，整次拒绝。"""
    mgr, path = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 50},
    })
    v = mgr.current().version
    path.write_text(json.dumps({"flags": {"ghost": {"force_on": ["a"]}}}))
    with pytest.raises(ConfigRejectionError) as ei:
        mgr.reload()
    assert ei.value.reason is RejectionReason.INVALID_KEY
    assert mgr.current().version == v
    print(f"[test] undeclared flag rejected path={ei.value.path}, serving={v}")


def test_legacy_config_without_new_fields(schema, tmp_path):
    """老配置（只有 enabled/percentage）行为不变：名单空、不整体关闭。"""
    mgr, _ = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 50},
        "beta": {"enabled": True},
    })
    snap = mgr.current()
    assert snap.flags["new_ui"].force_on == frozenset()
    assert snap.flags["new_ui"].force_off == frozenset()
    assert snap.flags["new_ui"].kill_switch is False
    d = snap.decide("new_ui", "user-9")
    assert d["reason"] in ("rollout_in", "rollout_out")
    # 纯布尔开关不受新机制影响
    assert snap.decide("beta")["reason"] == "boolean_on"
    print(f"[test] legacy config unaffected version={snap.version} reason={d['reason']}")


# ---------- 批量判定 ----------

def test_batch_single_version_and_reasons(schema, tmp_path):
    """整批同一版本，每个主体带判定依据。"""
    mgr, _ = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 50,
                   "force_on": ["vip-1"], "force_off": ["blocked-1"]},
    })
    snap = mgr.current()
    out = snap.decide_batch("new_ui", ["vip-1", "blocked-1", "user-1"])
    assert out["version"] == snap.version
    assert out["flag"] == "new_ui"
    assert [r["reason"] for r in out["results"]] == ["force_on", "force_off", out["results"][2]["reason"]]
    assert {r["version"] for r in out["results"]} == {snap.version}
    assert out["results"][2]["reason"] in ("rollout_in", "rollout_out")
    print(f"[test] batch version={out['version']} "
          f"reasons={[r['reason'] for r in out['results']]}")


def test_batch_empty_and_duplicates(schema, tmp_path):
    """空批次返回空结果但仍带版本；重复主体各返回一条且完全一致。"""
    mgr, _ = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 50, "force_on": ["vip-1"]},
    })
    snap = mgr.current()
    empty = snap.decide_batch("new_ui", [])
    assert empty["results"] == [] and empty["version"] == snap.version

    out = snap.decide_batch("new_ui", ["vip-1", "user-2", "vip-1", "user-2"])
    assert len(out["results"]) == 4
    assert out["results"][0] == out["results"][2]
    assert out["results"][1] == out["results"][3]
    assert out["results"][0]["reason"] == "force_on"
    print(f"[test] batch empty/dup ok version={snap.version} "
          f"reasons={[r['reason'] for r in out['results']]}")


def test_batch_unknown_flag(schema, tmp_path):
    mgr, _ = _manager(schema, tmp_path, {"new_ui": {"enabled": True, "percentage": 1}})
    out = mgr.current().decide_batch("nope", ["a", "b"])
    assert all(r["reason"] == "unknown_flag" and r["enabled"] is False for r in out["results"])
    assert out["version"] == mgr.current().version


def test_batch_scale_thousands(schema, tmp_path):
    """几千主体：结果正确、耗时平稳、无超线性恶化。"""
    mgr, _ = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 50,
                   "force_on": ["vip-1"], "force_off": ["blocked-1"], "kill_switch": False},
    })
    snap = mgr.current()
    subjects = [f"user-{i}" for i in range(5000)] + ["vip-1", "blocked-1"]
    import time
    t0 = time.perf_counter()
    out = snap.decide_batch("new_ui", subjects)
    elapsed = time.perf_counter() - t0
    assert len(out["results"]) == len(subjects)
    assert out["results"][-2]["reason"] == "force_on"
    assert out["results"][-1]["reason"] == "force_off"
    # 与逐个判定结果一致
    for s, r in zip(subjects, out["results"]):
        assert r == snap.decide("new_ui", s)
    print(f"[test] batch n={len(subjects)} elapsed={elapsed*1000:.1f}ms "
          f"version={out['version']}")
    assert elapsed < 2.0


def test_batch_duplicates_scale_without_blowup(schema, tmp_path):
    """高度重复批次不应越批量越慢（桶位按主体缓存，只算一次哈希）。"""
    mgr, _ = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 50},
    })
    snap = mgr.current()
    import time
    subjects = ["same-user"] * 5000
    t0 = time.perf_counter()
    out = snap.decide_batch("new_ui", subjects)
    elapsed = time.perf_counter() - t0
    assert len(out["results"]) == 5000
    assert len({id(r) for r in out["results"]}) == 1 or all(
        r == out["results"][0] for r in out["results"])
    print(f"[test] batch 5000 duplicates elapsed={elapsed*1000:.1f}ms "
          f"version={out['version']}")
    assert elapsed < 1.0


def test_batch_consistent_under_concurrent_reload(schema, tmp_path, monkeypatch):
    """一边批量判定一边热更新：任何一批结果只能全在同一版本。

    通过在单主体判定期间注入可同步的屏障，确保批量处理过程中真实发生快照切换；
    即便如此，每批也必须整批同版本（批量入口只取一次快照）。
    """
    mgr, path = _manager(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 50},
    })
    v1 = mgr.current().version

    entered = threading.Event()
    release = threading.Event()
    real_decide = type(mgr.current()).decide
    call_count = {"n": 0}

    def patched_decide(self, flag, subject=None):
        call_count["n"] += 1
        if call_count["n"] == 2:
            entered.set()
            release.wait(timeout=2.0)
        return real_decide(self, flag, subject)

    monkeypatch.setattr(type(mgr.current()), "decide", patched_decide)
    batch_versions = []
    errors = []

    def worker_batch():
        out = mgr.current().decide_batch("new_ui", [f"user-{i}" for i in range(200)])
        vs = {r["version"] for r in out["results"]}
        batch_versions.append((out["version"], vs))
        if vs != {out["version"]}:
            errors.append("批量结果串版本")

    t = threading.Thread(target=worker_batch)
    t.start()
    assert entered.wait(timeout=2.0)
    # 此刻批量进行中：写入并热更新到新版本
    path.write_text(json.dumps({"flags": {
        "new_ui": {"enabled": True, "percentage": 80}}}))
    mgr.reload()
    v2 = mgr.current().version
    assert v2 != v1
    release.set()
    t.join()

    assert not errors
    top_version, all_versions = batch_versions[0]
    assert all_versions == {top_version}
    assert top_version in (v1, v2)
    print(f"[test] batch under reload: whole batch stayed on {top_version} "
          f"(v1={v1}, v2={v2})")


# ---------- 批量 HTTP 入口 ----------

def _client(schema, tmp_path, flag_cfg):
    mgr, _ = _manager(schema, tmp_path, flag_cfg)
    return TestClient(create_app(mgr)), mgr


def test_batch_endpoint_ok(schema, tmp_path):
    client, mgr = _client(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 50,
                   "force_on": ["vip-1"], "kill_switch": False},
    })
    resp = client.post("/flags/new_ui/batch", json={"subjects": ["vip-1", "user-2"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == mgr.current().version
    assert body["results"][0]["reason"] == "force_on"
    assert body["results"][1]["reason"] in ("rollout_in", "rollout_out")
    print(f"[test] HTTP batch version={body['version']} "
          f"reasons={[r['reason'] for r in body['results']]}")


def test_batch_endpoint_empty_and_bad_payload(schema, tmp_path):
    client, _ = _client(schema, tmp_path, {"new_ui": {"enabled": True, "percentage": 1}})
    resp = client.post("/flags/new_ui/batch", json={"subjects": []})
    assert resp.status_code == 200
    assert resp.json()["results"] == []
    # 元素不是字符串 -> 422（pydantic 严格校验，不做隐式转换）
    resp = client.post("/flags/new_ui/batch", json={"subjects": ["ok", 1]})
    assert resp.status_code == 422
    # 缺字段 / 类型不对 -> 422
    assert client.post("/flags/new_ui/batch", json={}).status_code == 422
    assert client.post("/flags/new_ui/batch", json={"subjects": "a"}).status_code == 422
    print("[test] HTTP batch empty=200, bad payloads=422")


def test_batch_endpoint_kill_switch_visible(schema, tmp_path):
    client, mgr = _client(schema, tmp_path, {
        "new_ui": {"enabled": True, "percentage": 100, "kill_switch": True},
    })
    body = client.post("/flags/new_ui/batch", json={"subjects": ["a", "b"]}).json()
    assert all(r["reason"] == "kill_switch" and r["enabled"] is False
               for r in body["results"])
    print(f"[test] HTTP batch kill_switch version={body['version']}")
