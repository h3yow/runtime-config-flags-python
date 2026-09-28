"""新行为测试：整体关闭、强制名单、优先级叠加、名单校验原子拒绝。"""

import json

import pytest

from config_system.errors import ConfigRejectionError, RejectionReason
from config_system.manager import ConfigManager
from config_system.rollout import in_rollout
from config_system.schema import ConfigSchema, FieldSpec, FlagSpec
from fastapi.testclient import TestClient
from config_system.service import create_app


@pytest.fixture
def schema():
    return ConfigSchema(
        fields=(FieldSpec(key="limit", type="int", default=10),),
        flags=(FlagSpec(name="beta"), FlagSpec(name="new_ui")),
    )


def _write(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return str(path)


def _client(schema, tmp_path, data):
    path = _write(tmp_path, data)
    mgr = ConfigManager(schema, file_path=path, environ={})
    return TestClient(create_app(mgr)), mgr, path


# ---------- 整体关闭 global_off 与比例叠加 ----------

def test_global_off_overrides_percentage_100(schema, tmp_path):
    """整体关闭优先级最高：比例 100 + force_on 也一律关闭。"""
    client, mgr, _ = _client(schema, tmp_path, {
        "flags": {"new_ui": {"enabled": True, "percentage": 100,
                             "global_off": True, "force_on": ["vip-1"]}},
    })
    for subject in ["anyone", "vip-1", ""]:
        d = client.get("/flags/new_ui", params={"subject": subject}).json()
        assert d["enabled"] is False
        assert d["reason"] == "global_off"
    print(f"[test] global_off@100: version={mgr.current().version} reason=global_off "
          f"(percentage=100 与 force_on 均被压过)")


def test_global_off_overrides_percentage_0_boundary(schema, tmp_path):
    """比例 0 边界 + 整体关闭：结果确定、依据仍是 global_off 而非 rollout_out。"""
    client, mgr, _ = _client(schema, tmp_path, {
        "flags": {"new_ui": {"enabled": True, "percentage": 0, "global_off": True}},
    })
    d = client.get("/flags/new_ui", params={"subject": "u-9"}).json()
    assert d["enabled"] is False
    assert d["reason"] == "global_off"
    print(f"[test] global_off@0: version={d['version']} reason={d['reason']}")


def test_percentage_boundaries_without_global_off_unchanged(schema, tmp_path):
    """不开启整体关闭时，0 恒关、100 恒开（边界不回归）。"""
    client, _, _ = _client(schema, tmp_path, {
        "flags": {"new_ui": {"enabled": True, "percentage": 0}},
    })
    d0 = client.get("/flags/new_ui", params={"subject": "u"}).json()
    assert d0["enabled"] is False and d0["reason"] == "rollout_out"

    data = json.loads((tmp_path / "config.json").read_text())
    data["flags"]["new_ui"]["percentage"] = 100
    _write(tmp_path, data)
    client.post("/config/reload")
    d100 = client.get("/flags/new_ui", params={"subject": "u"}).json()
    assert d100["enabled"] is True and d100["reason"] == "rollout_in"
    print(f"[test] boundary 0%={d0['reason']} 100%={d100['reason']} versions="
          f"{d0['version']}->{d100['version']}")


# ---------- 两个名单与比例叠加 ----------

def test_force_on_stable_regardless_of_percentage(schema, tmp_path):
    """force_on 主体稳定命中：比例调到 0 仍开，且依据可区分。"""
    client, mgr, _ = _client(schema, tmp_path, {
        "flags": {"new_ui": {"enabled": True, "percentage": 50,
                             "force_on": ["vip-1", "vip-2"]}},
    })
    for pct in (50, 0, 10):
        data = {"flags": {"new_ui": {"enabled": True, "percentage": pct,
                                     "force_on": ["vip-1", "vip-2"]}}}
        _write(tmp_path, data)
        client.post("/config/reload")
        for vip in ("vip-1", "vip-2"):
            d = client.get("/flags/new_ui", params={"subject": vip}).json()
            assert d["enabled"] is True and d["reason"] == "force_on"
    print(f"[test] force_on stable across percentage 50/0/10: "
          f"version={mgr.current().version}")


def test_force_off_overrides_force_on_and_percentage(schema, tmp_path):
    """同时命中两名单 -> 以关闭为准；force_off 也压过比例命中。"""
    # 找一个在 50% 下本来会被比例选中的普通主体
    rolled_in = next(f"user-{i}" for i in range(2000)
                     if in_rollout("new_ui", f"user-{i}",
                                   "runtime-config-flags", 50))
    client, mgr, _ = _client(schema, tmp_path, {
        "flags": {"new_ui": {
            "enabled": True, "percentage": 50,
            "force_on": ["both", rolled_in],
            "force_off": ["both", rolled_in],
        }},
    })
    d = client.get("/flags/new_ui", params={"subject": "both"}).json()
    assert d["enabled"] is False and d["reason"] == "force_off"
    d2 = client.get("/flags/new_ui", params={"subject": rolled_in}).json()
    assert d2["enabled"] is False and d2["reason"] == "force_off"
    print(f"[test] both-lists -> force_off: version={d['version']} "
          f"subjects=both,{rolled_in}")


def test_normal_subjects_still_follow_percentage(schema, tmp_path):
    """名单外主体继续按比例走，加名单不扰动原有分组。"""
    subjects = [f"user-{i}" for i in range(300)]
    data_v1 = {"flags": {"new_ui": {"enabled": True, "percentage": 30}}}
    client, _, _ = _client(schema, tmp_path, data_v1)
    before = {s: client.get("/flags/new_ui", params={"subject": s}).json()["enabled"]
              for s in subjects}
    # 热更新：只加名单，比例不变
    data_v2 = {"flags": {"new_ui": {"enabled": True, "percentage": 30,
                                    "force_on": ["vip-x"], "force_off": ["blocked-y"]}}}
    _write(tmp_path, data_v2)
    resp = client.post("/config/reload")
    assert resp.status_code == 200
    after = {s: client.get("/flags/new_ui", params={"subject": s}).json()["enabled"]
             for s in subjects}
    assert before == after
    print(f"[test] lists added, non-listed grouping unchanged: "
          f"{sum(before.values())}/{len(subjects)} enabled")


def test_percentage_increase_does_not_kick_out_non_listed(schema, tmp_path):
    """比例调大时名单外主体只能进不能出（前缀桶特性不被名单破坏）。"""
    data = {"flags": {"new_ui": {"enabled": True, "percentage": 20,
                                 "force_on": ["vip-1"], "force_off": ["bad-1"]}}}
    client, _, _ = _client(schema, tmp_path, data)
    subjects = [f"user-{i}" for i in range(1000)]
    on_20 = {s for s in subjects
             if client.get("/flags/new_ui", params={"subject": s}).json()["enabled"]}
    data["flags"]["new_ui"]["percentage"] = 60
    _write(tmp_path, data)
    client.post("/config/reload")
    on_60 = {s for s in subjects
             if client.get("/flags/new_ui", params={"subject": s}).json()["enabled"]}
    assert on_20 <= on_60
    # 名单主体不受影响
    assert client.get("/flags/new_ui", params={"subject": "vip-1"}).json()["reason"] == "force_on"
    assert client.get("/flags/new_ui", params={"subject": "bad-1"}).json()["reason"] == "force_off"
    print(f"[test] expand 20->60: {len(on_20)} -> {len(on_60)}, kicked="
          f"{len(on_20 - on_60)}")


def test_global_off_beats_lists_and_boolean(schema, tmp_path):
    """纯布尔开关 + 名单 + 整体关闭：global_off 最高。"""
    client, _, _ = _client(schema, tmp_path, {
        "flags": {"beta": {"enabled": True, "global_off": True,
                           "force_on": ["vip"]}},
    })
    d = client.get("/flags/beta", params={"subject": "vip"}).json()
    assert d["enabled"] is False and d["reason"] == "global_off"


# ---------- 名单写坏：整次更新原子拒绝 ----------

def test_bad_list_type_rejects_entire_reload(schema, tmp_path):
    client, mgr, path = _client(schema, tmp_path, {
        "fields": {"limit": 10},
        "flags": {"new_ui": {"enabled": True, "percentage": 50,
                             "force_on": ["vip-1"]}},
    })
    v_good = mgr.current().version
    # force_on 写成字符串而非数组
    with open(path, "w") as fh:
        json.dump({"fields": {"limit": 20},
                   "flags": {"new_ui": {"enabled": True, "percentage": 50,
                                        "force_on": "vip-1"}}}, fh)
    resp = client.post("/config/reload")
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["reason"] == "type_mismatch"
    assert err["path"] == "flags.new_ui.force_on"
    assert err["serving_version"] == v_good
    # 整次更新全部不生效：limit 仍是旧值，名单仍是旧名单
    assert mgr.current().version == v_good
    assert mgr.current().get("limit") == 10
    assert client.get("/flags/new_ui", params={"subject": "vip-1"}).json()["reason"] == "force_on"
    print(f"[test] bad force_on rejected atomically, serving={v_good} "
          f"reason={err['reason']} path={err['path']}")


def test_bad_list_element_type_rejected(schema, tmp_path):
    path = _write(tmp_path, {"fields": {"limit": 10}})
    mgr = ConfigManager(schema, file_path=path, environ={})
    with open(path, "w") as fh:
        json.dump({"fields": {"limit": 10},
                   "flags": {"new_ui": {"enabled": True,
                                        "force_off": [1, 2]}}}, fh)
    with pytest.raises(ConfigRejectionError) as ei:
        mgr.reload()
    assert ei.value.reason is RejectionReason.TYPE_MISMATCH
    assert ei.value.path == "flags.new_ui.force_off[0]"


def test_list_for_undeclared_flag_rejected(schema, tmp_path):
    path = _write(tmp_path, {"fields": {"limit": 10}})
    mgr = ConfigManager(schema, file_path=path, environ={})
    with open(path, "w") as fh:
        json.dump({"fields": {"limit": 10},
                   "flags": {"ghost": {"force_on": ["x"]}}}, fh)
    with pytest.raises(ConfigRejectionError) as ei:
        mgr.reload()
    assert ei.value.reason is RejectionReason.INVALID_KEY
    print(f"[test] undeclared flag with list rejected: {ei.value.path}")


def test_bad_global_off_type_rejected(schema, tmp_path):
    path = _write(tmp_path, {"fields": {"limit": 10}})
    mgr = ConfigManager(schema, file_path=path, environ={})
    with open(path, "w") as fh:
        json.dump({"fields": {"limit": 10},
                   "flags": {"new_ui": {"global_off": "yes"}}}, fh)
    with pytest.raises(ConfigRejectionError) as ei:
        mgr.reload()
    assert ei.value.reason is RejectionReason.TYPE_MISMATCH
    assert ei.value.path == "flags.new_ui.global_off"
