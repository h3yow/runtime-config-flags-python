"""纯布尔开关（未设灰度比例）叠加整体关闭 / 强制名单的回归测试。

背景：上一版 kill_switch / force_on / force_off 只对设了比例的灰度开关生效，
纯布尔开关上配了也完全被忽略。本文件锁定修复后的行为。

所有用例均打印配置版本号与判定依据（reason），方便出问题时对照日志。
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
        flags=(FlagSpec(name="plain_on"), FlagSpec(name="plain_off")),
    )


def _manager(schema, tmp_path, flag_cfg):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"flags": flag_cfg}))
    return ConfigManager(schema, file_path=str(path), environ={}), path


# ---------- 整体关闭对纯布尔开关生效 ----------

def test_kill_switch_applies_to_boolean_flag(schema, tmp_path):
    """没设比例的开关被整体关掉：所有主体一律关闭，reason 是 kill_switch 而非 boolean_off。"""
    mgr, _ = _manager(schema, tmp_path, {
        "plain_on": {"enabled": True, "kill_switch": True},
    })
    snap = mgr.current()
    for subject in ["user-1", "user-2", ""]:
        d = snap.decide("plain_on", subject)
        assert d["enabled"] is False
        assert d["reason"] == "kill_switch", "整体关闭必须可与 boolean_off 区分"
    print(f"[test] boolean+kill_switch version={snap.version} -> all kill_switch")


def test_kill_switch_beats_force_on_on_boolean_flag(schema, tmp_path):
    """纯布尔开关：整体关闭优先级高于强制打开名单。"""
    mgr, _ = _manager(schema, tmp_path, {
        "plain_on": {"enabled": True, "kill_switch": True, "force_on": ["vip-1"]},
    })
    d = mgr.current().decide("plain_on", "vip-1")
    assert d["enabled"] is False and d["reason"] == "kill_switch"
    print(f"[test] boolean kill>force_on version={d['version']} reason={d['reason']}")


def test_kill_switch_release_restores_boolean_state(schema, tmp_path):
    """整体关闭解除后回到开关原本的开/关状态。"""
    mgr, path = _manager(schema, tmp_path, {
        "plain_on": {"enabled": True, "kill_switch": True},
        "plain_off": {"enabled": False, "kill_switch": True},
    })
    assert mgr.current().decide("plain_on", "u")["reason"] == "kill_switch"
    assert mgr.current().decide("plain_off", "u")["reason"] == "kill_switch"

    path.write_text(json.dumps({"flags": {
        "plain_on": {"enabled": True, "kill_switch": False},
        "plain_off": {"enabled": False, "kill_switch": False},
    }}))
    snap = mgr.reload()
    d_on = snap.decide("plain_on", "u")
    d_off = snap.decide("plain_off", "u")
    assert d_on["enabled"] is True and d_on["reason"] == "boolean_on"
    assert d_off["enabled"] is False and d_off["reason"] == "boolean_off"
    print(f"[test] kill released on boolean flags version={snap.version} "
          f"reasons={d_on['reason']},{d_off['reason']}")


# ---------- 强制名单对纯布尔开关生效 ----------

def test_force_lists_apply_to_boolean_flag(schema, tmp_path):
    """没设比例的开关：force_on 把关着的开关对名单内主体打开，force_off 反之。"""
    mgr, _ = _manager(schema, tmp_path, {
        "plain_off": {"enabled": False, "force_on": ["vip-1"]},
        "plain_on": {"enabled": True, "force_off": ["blocked-1"]},
    })
    snap = mgr.current()
    d1 = snap.decide("plain_off", "vip-1")
    assert d1["enabled"] is True and d1["reason"] == "force_on"
    d2 = snap.decide("plain_off", "ordinary-1")
    assert d2["enabled"] is False and d2["reason"] == "boolean_off"
    d3 = snap.decide("plain_on", "blocked-1")
    assert d3["enabled"] is False and d3["reason"] == "force_off"
    d4 = snap.decide("plain_on", "ordinary-2")
    assert d4["enabled"] is True and d4["reason"] == "boolean_on"
    print(f"[test] boolean force lists version={snap.version} "
          f"reasons={d1['reason']},{d2['reason']},{d3['reason']},{d4['reason']}")


def test_both_lists_close_wins_on_boolean_flag(schema, tmp_path):
    """纯布尔开关上同一主体同时命中两个名单：以关闭为准，结果稳定不摇摆。"""
    mgr, _ = _manager(schema, tmp_path, {
        "plain_on": {"enabled": True,
                     "force_on": ["conflict-1"], "force_off": ["conflict-1"]},
    })
    snap = mgr.current()
    reasons = {snap.decide("plain_on", "conflict-1")["reason"] for _ in range(10)}
    assert reasons == {"force_off"}
    assert snap.decide("plain_on", "conflict-1")["enabled"] is False
    print(f"[test] boolean both-lists version={snap.version} -> force_off (close wins)")


def test_boolean_flag_without_subject_uses_lists_with_empty_subject(schema, tmp_path):
    """不传主体时按空主体判定：空主体在名单里才命中，否则回落到布尔状态。"""
    mgr, _ = _manager(schema, tmp_path, {
        "plain_off": {"enabled": False, "force_on": ["vip-1"]},
    })
    snap = mgr.current()
    d = snap.decide("plain_off")
    assert d["enabled"] is False and d["reason"] == "boolean_off"
    d_vip = snap.decide("plain_off", "vip-1")
    assert d_vip["enabled"] is True and d_vip["reason"] == "force_on"
    print(f"[test] boolean no-subject version={snap.version} "
          f"reasons={d['reason']},{d_vip['reason']}")


# ---------- 兼容与边界 ----------

def test_legacy_boolean_only_config_unchanged(schema, tmp_path):
    """只写 enabled 的老配置：行为与旧版完全一致（不受新字段缺省值影响）。"""
    mgr, _ = _manager(schema, tmp_path, {
        "plain_on": {"enabled": True},
        "plain_off": {"enabled": False},
    })
    snap = mgr.current()
    assert snap.flags["plain_on"].force_on == frozenset()
    assert snap.flags["plain_on"].force_off == frozenset()
    assert snap.flags["plain_on"].kill_switch is False
    d_on = snap.decide("plain_on", "anyone")
    d_off = snap.decide("plain_off", "anyone")
    assert (d_on["enabled"], d_on["reason"]) == (True, "boolean_on")
    assert (d_off["enabled"], d_off["reason"]) == (False, "boolean_off")
    print(f"[test] legacy boolean-only version={snap.version} "
          f"reasons={d_on['reason']},{d_off['reason']}")


def test_bad_fields_on_boolean_flag_reject_entire_update(schema, tmp_path):
    """纯布尔开关上名单/整体关闭写坏：整次更新拒绝，旧版本继续服役。"""
    mgr, path = _manager(schema, tmp_path, {
        "plain_on": {"enabled": True, "force_on": ["vip-1"]},
    })
    good_version = mgr.current().version
    for bad in (
        {"force_on": "not-a-list"},
        {"force_off": [123]},
        {"kill_switch": "yes"},
        {"force_on": [None]},
    ):
        path.write_text(json.dumps({"flags": {"plain_on": {"enabled": True, **bad}}}))
        with pytest.raises(ConfigRejectionError) as ei:
            mgr.reload()
        assert ei.value.reason is RejectionReason.TYPE_MISMATCH
        assert mgr.current().version == good_version
        # 旧版本名单仍然有效，不是只生效一半
        assert mgr.current().decide("plain_on", "vip-1")["reason"] == "force_on"
    print(f"[test] bad fields on boolean flag rejected, serving={good_version}")


def test_boolean_flag_mixed_with_rollout_flag_in_one_update(schema, tmp_path):
    """同一次更新里灰度开关写坏、纯布尔开关写好：整次拒绝，两个开关都保持旧版本。"""
    schema2 = ConfigSchema(
        flags=(FlagSpec(name="plain_on"), FlagSpec(name="grad")),
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"flags": {
        "plain_on": {"enabled": True},
        "grad": {"enabled": True, "percentage": 50},
    }}))
    mgr = ConfigManager(schema2, file_path=str(path), environ={})
    good_version = mgr.current().version

    path.write_text(json.dumps({"flags": {
        "plain_on": {"enabled": False, "kill_switch": True},   # 本身合法
        "grad": {"enabled": True, "percentage": 50, "force_on": "oops"},  # 写坏
    }}))
    with pytest.raises(ConfigRejectionError):
        mgr.reload()
    snap = mgr.current()
    assert snap.version == good_version
    # 纯布尔开关的改动也不能只生效一半
    assert snap.decide("plain_on", "u")["reason"] == "boolean_on"
    print(f"[test] atomic reject across flags, serving={snap.version}")


def test_percentage_boundary_with_kill_and_lists_is_determinate(schema, tmp_path):
    """比例 0/100 叠加整体关闭与名单：结果唯一确定，reason 可区分。"""
    mgr, _ = _manager(schema, tmp_path, {
        "plain_on": {"enabled": True, "percentage": 0, "kill_switch": True,
                     "force_on": ["vip-1"]},
        "plain_off": {"enabled": True, "percentage": 100,
                      "force_on": ["vip-1"], "force_off": ["vip-1"]},
    })
    snap = mgr.current()
    d0 = snap.decide("plain_on", "vip-1")
    assert (d0["enabled"], d0["reason"]) == (False, "kill_switch")
    d100 = snap.decide("plain_off", "vip-1")
    assert (d100["enabled"], d100["reason"]) == (False, "force_off")
    d100_other = snap.decide("plain_off", "ordinary-1")
    assert (d100_other["enabled"], d100_other["reason"]) == (True, "rollout_in")
    print(f"[test] boundary+kill+lists version={snap.version} "
          f"reasons={d0['reason']},{d100['reason']},{d100_other['reason']}")
