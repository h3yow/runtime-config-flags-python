"""纯布尔开关（未设比例）叠加整体关闭 / 强制名单的回归测试。

背景：上一版 decide() 对 percentage<0 的开关提前返回，导致
kill_switch / force_on / force_off 对纯布尔开关完全不生效。
本文件锁定修复后的行为；所有用例打印配置版本号与判定依据。
"""

import json

import pytest

from config_system.errors import ConfigRejectionError, RejectionReason
from config_system.manager import ConfigManager
from config_system.schema import ConfigSchema, FieldSpec, FlagSpec


@pytest.fixture
def schema():
    return ConfigSchema(
        fields=(FieldSpec(key="greeting", type="str", default="hi"),),
        flags=(FlagSpec(name="beta"), FlagSpec(name="new_ui")),
    )


def _manager(schema, tmp_path, flag_cfg):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"flags": flag_cfg}))
    return ConfigManager(schema, file_path=str(path), environ={}), path


# ---------- 整体关闭对纯布尔开关生效 ----------

def test_kill_switch_applies_to_boolean_flag(schema, tmp_path):
    """没设比例的开关被整体关掉：所有主体一律关闭，reason 是 kill_switch。"""
    mgr, _ = _manager(schema, tmp_path, {
        "beta": {"enabled": True, "kill_switch": True},
    })
    snap = mgr.current()
    for subject in ["user-1", "vip-1", "", None]:
        d = snap.decide("beta", subject)
        assert d["enabled"] is False
        assert d["reason"] == "kill_switch"
    print(f"[test] boolean+kill_switch version={snap.version} -> all kill_switch")


def test_kill_switch_reason_distinct_from_boolean_off(schema, tmp_path):
    """开关本来就是关的 + 整体关闭：reason 必须是 kill_switch，不是 boolean_off。"""
    mgr, _ = _manager(schema, tmp_path, {
        "beta": {"enabled": False, "kill_switch": True},
    })
    d = mgr.current().decide("beta", "user-1")
    assert d["enabled"] is False
    assert d["reason"] == "kill_switch"
    print(f"[test] off+kill_switch version={d['version']} reason={d['reason']} "
          f"(distinguishable from boolean_off)")


def test_kill_switch_release_restores_boolean_state(schema, tmp_path):
    """整体关闭解除后回到开关原本的开/关状态。"""
    mgr, path = _manager(schema, tmp_path, {
        "beta": {"enabled": True, "kill_switch": True},
        "new_ui": {"enabled": False},
    })
    assert mgr.current().decide("beta", "u")["reason"] == "kill_switch"
    v1 = mgr.current().version

    path.write_text(json.dumps({"flags": {
        "beta": {"enabled": True, "kill_switch": False},
        "new_ui": {"enabled": False},
    }}))
    snap = mgr.reload()
    assert snap.version != v1
    d_on = snap.decide("beta", "u")
    d_off = snap.decide("new_ui", "u")
    assert d_on["enabled"] is True and d_on["reason"] == "boolean_on"
    assert d_off["enabled"] is False and d_off["reason"] == "boolean_off"
    print(f"[test] kill released on boolean flags: {v1}->{snap.version} "
          f"beta={d_on['reason']} new_ui={d_off['reason']}")


# ---------- 强制名单对纯布尔开关生效 ----------

def test_force_on_opens_boolean_off_flag(schema, tmp_path):
    """开关整体关着，force_on 名单里的主体仍然打开。"""
    mgr, _ = _manager(schema, tmp_path, {
        "beta": {"enabled": False, "force_on": ["vip-1"]},
    })
    snap = mgr.current()
    d_vip = snap.decide("beta", "vip-1")
    d_other = snap.decide("beta", "ordinary-1")
    assert d_vip["enabled"] is True and d_vip["reason"] == "force_on"
    assert d_other["enabled"] is False and d_other["reason"] == "boolean_off"
    print(f"[test] force_on@boolean_off version={snap.version} "
          f"vip={d_vip['reason']} other={d_other['reason']}")


def test_force_off_closes_boolean_on_flag(schema, tmp_path):
    """开关整体开着，force_off 名单里的主体被关掉。"""
    mgr, _ = _manager(schema, tmp_path, {
        "beta": {"enabled": True, "force_off": ["blocked-1"]},
    })
    snap = mgr.current()
    d_blocked = snap.decide("beta", "blocked-1")
    d_other = snap.decide("beta", "ordinary-1")
    assert d_blocked["enabled"] is False and d_blocked["reason"] == "force_off"
    assert d_other["enabled"] is True and d_other["reason"] == "boolean_on"
    print(f"[test] force_off@boolean_on version={snap.version} "
          f"blocked={d_blocked['reason']} other={d_other['reason']}")


def test_both_lists_on_boolean_flag_close_wins(schema, tmp_path):
    """纯布尔开关上同一主体同时命中两名单：恒为关闭，反复判定不摇摆。"""
    mgr, _ = _manager(schema, tmp_path, {
        "beta": {"enabled": True,
                 "force_on": ["conflict-1"], "force_off": ["conflict-1"]},
    })
    snap = mgr.current()
    results = {(snap.decide("beta", "conflict-1")["enabled"],
                snap.decide("beta", "conflict-1")["reason"]) for _ in range(20)}
    assert results == {(False, "force_off")}
    print(f"[test] boolean both-lists version={snap.version} -> force_off (stable)")


def test_kill_switch_beats_force_lists_on_boolean_flag(schema, tmp_path):
    """纯布尔开关：整体关闭优先于两个名单。"""
    mgr, _ = _manager(schema, tmp_path, {
        "beta": {"enabled": True, "force_on": ["vip-1"],
                 "force_off": ["blocked-1"], "kill_switch": True},
    })
    snap = mgr.current()
    for subject in ["vip-1", "blocked-1", "anyone"]:
        d = snap.decide("beta", subject)
        assert d["enabled"] is False and d["reason"] == "kill_switch"
    print(f"[test] boolean kill>lists version={snap.version} -> all kill_switch")


# ---------- 批量判定覆盖纯布尔开关 ----------

def test_batch_on_boolean_flag_with_kill_and_lists(schema, tmp_path):
    """批量判定：纯布尔开关叠加整体关闭与名单，整批同版本、理由正确。"""
    mgr, _ = _manager(schema, tmp_path, {
        "beta": {"enabled": True, "force_off": ["blocked-1"], "kill_switch": False},
    })
    snap = mgr.current()
    out = snap.decide_batch("beta", ["blocked-1", "user-1"])
    assert out["version"] == snap.version
    assert [r["reason"] for r in out["results"]] == ["force_off", "boolean_on"]
    print(f"[test] batch boolean version={out['version']} "
          f"reasons={[r['reason'] for r in out['results']]}")


# ---------- 兼容与边界 ----------

def test_legacy_boolean_only_config_unchanged(schema, tmp_path):
    """老配置（只写 enabled，无新字段）行为与旧版完全一致。"""
    mgr, _ = _manager(schema, tmp_path, {
        "beta": {"enabled": True},
        "new_ui": {"enabled": False},
    })
    snap = mgr.current()
    assert snap.flags["beta"].kill_switch is False
    assert snap.flags["beta"].force_on == frozenset()
    assert snap.flags["beta"].force_off == frozenset()
    assert snap.decide("beta", "anyone")["reason"] == "boolean_on"
    assert snap.decide("new_ui", "anyone")["reason"] == "boolean_off"
    print(f"[test] legacy boolean-only unchanged version={snap.version}")


def test_bad_fields_on_boolean_flag_reject_entire_update(schema, tmp_path):
    """纯布尔开关上的名单/整体关闭写坏：整次更新拒绝，旧版本继续服役。"""
    mgr, path = _manager(schema, tmp_path, {
        "beta": {"enabled": True, "force_off": ["blocked-1"]},
    })
    good_version = mgr.current().version
    for bad in (
        {"force_on": "not-a-list"},
        {"force_off": [123]},
        {"kill_switch": 1},
    ):
        path.write_text(json.dumps({"flags": {"beta": {"enabled": True, **bad}}}))
        with pytest.raises(ConfigRejectionError) as ei:
            mgr.reload()
        assert ei.value.reason is RejectionReason.TYPE_MISMATCH
        assert mgr.current().version == good_version
        # 旧版本名单仍然有效，没有只生效一半
        assert mgr.current().decide("beta", "blocked-1")["reason"] == "force_off"
    print(f"[test] bad fields on boolean flag rejected, serving={good_version}")


def test_mixed_boolean_and_rollout_flags_independent(schema, tmp_path):
    """同一配置里纯布尔开关与灰度开关各自套用统一优先级，互不影响。"""
    mgr, _ = _manager(schema, tmp_path, {
        "beta": {"enabled": True, "kill_switch": True},
        "new_ui": {"enabled": True, "percentage": 100, "force_off": ["blocked-1"]},
    })
    snap = mgr.current()
    assert snap.decide("beta", "anyone")["reason"] == "kill_switch"
    d = snap.decide("new_ui", "blocked-1")
    assert d["enabled"] is False and d["reason"] == "force_off"
    d2 = snap.decide("new_ui", "user-1")
    assert d2["enabled"] is True and d2["reason"] == "rollout_in"
    print(f"[test] mixed flags version={snap.version} "
          f"beta=kill_switch new_ui={d['reason']}/{d2['reason']}")
