"""分层合并与校验：每种失败原因必须可区分。"""

import pytest

from config_system.errors import ConfigRejectionError, RejectionReason
from config_system.schema import ConfigSchema, FieldSpec, FlagSpec
from config_system.validator import build_config, merge_layers, validate


def _schema():
    return ConfigSchema(
        fields=(
            FieldSpec(key="greeting", type="str", default="hello"),
            FieldSpec(key="limit", type="int", required=True, min_value=1, max_value=1000),
            FieldSpec(key="mode", type="str", default="a", choices=("a", "b")),
            FieldSpec(key="api_key", type="secret", required=True),
            FieldSpec(key="verbose", type="bool", default=False),
        ),
        flags=(FlagSpec(name="beta"),),
    )


def _layer(fields=None, flags=None):
    return {"fields": fields or {}, "flags": flags or {}}


def test_merge_priority_env_over_file_over_defaults():
    defaults = _layer(fields={"limit": 5, "greeting": "d"})
    file_layer = _layer(fields={"limit": 10})
    env_layer = _layer(fields={"greeting": "env"})
    merged = merge_layers(defaults, file_layer, env_layer)
    assert merged["fields"] == {"limit": 10, "greeting": "env"}


def test_merge_flags_deep():
    a = _layer(flags={"beta": {"enabled": True}})
    b = _layer(flags={"beta": {"percentage": 30}})
    merged = merge_layers(a, b)
    assert merged["flags"]["beta"] == {"enabled": True, "percentage": 30}


def test_valid_config_builds_snapshot():
    raw = _layer(fields={"limit": 100, "api_key": "s3cret"},
                 flags={"beta": {"enabled": True, "percentage": 20}})
    snap = build_config(raw, _schema())
    assert snap.version and len(snap.version) == 12
    assert snap.get("limit") == 100
    assert snap.flags["beta"].percentage == 20


def test_same_content_same_version():
    raw = _layer(fields={"limit": 1, "api_key": "k"})
    assert build_config(raw, _schema()).version == build_config(raw, _schema()).version


def test_missing_required():
    raw = _layer(fields={"api_key": "k"})  # 缺 limit
    with pytest.raises(ConfigRejectionError) as ei:
        validate(raw, _schema())
    assert ei.value.reason is RejectionReason.MISSING_REQUIRED
    assert "limit" in ei.value.path


def test_type_mismatch_str_for_int():
    raw = _layer(fields={"limit": "not-an-int", "api_key": "k"})
    with pytest.raises(ConfigRejectionError) as ei:
        validate(raw, _schema())
    assert ei.value.reason is RejectionReason.TYPE_MISMATCH


def test_bool_not_accepted_as_int():
    raw = _layer(fields={"limit": True, "api_key": "k"})
    with pytest.raises(ConfigRejectionError) as ei:
        validate(raw, _schema())
    assert ei.value.reason is RejectionReason.TYPE_MISMATCH


def test_illegal_value_out_of_range():
    raw = _layer(fields={"limit": 99999, "api_key": "k"})
    with pytest.raises(ConfigRejectionError) as ei:
        validate(raw, _schema())
    assert ei.value.reason is RejectionReason.ILLEGAL_VALUE


def test_illegal_choice():
    raw = _layer(fields={"limit": 1, "api_key": "k", "mode": "zzz"})
    with pytest.raises(ConfigRejectionError) as ei:
        validate(raw, _schema())
    assert ei.value.reason is RejectionReason.ILLEGAL_VALUE


def test_unknown_field_rejected():
    raw = _layer(fields={"limit": 1, "api_key": "k", "mystery": 1})
    with pytest.raises(ConfigRejectionError) as ei:
        validate(raw, _schema())
    assert ei.value.reason is RejectionReason.INVALID_KEY


def test_unknown_flag_rejected():
    raw = _layer(fields={"limit": 1, "api_key": "k"}, flags={"nope": {"enabled": True}})
    with pytest.raises(ConfigRejectionError) as ei:
        validate(raw, _schema())
    assert ei.value.reason is RejectionReason.INVALID_KEY


def test_flag_percentage_out_of_range():
    raw = _layer(fields={"limit": 1, "api_key": "k"},
                 flags={"beta": {"enabled": True, "percentage": 150}})
    with pytest.raises(ConfigRejectionError) as ei:
        validate(raw, _schema())
    assert ei.value.reason is RejectionReason.ILLEGAL_VALUE


def test_flag_enabled_type_mismatch():
    raw = _layer(fields={"limit": 1, "api_key": "k"},
                 flags={"beta": {"enabled": "yes"}})
    with pytest.raises(ConfigRejectionError) as ei:
        validate(raw, _schema())
    assert ei.value.reason is RejectionReason.TYPE_MISMATCH


def test_secret_redacted_in_public_view():
    raw = _layer(fields={"limit": 1, "api_key": "super-secret-value"})
    snap = build_config(raw, _schema())
    view = snap.public_view()
    assert view["fields"]["api_key"] == "***REDACTED***"
    assert "super-secret-value" not in str(view)
    # 内部读取仍可用
    assert snap.get("api_key") == "super-secret-value"
