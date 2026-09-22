"""来源层读取测试。"""

import json

import pytest

from config_system.errors import ConfigRejectionError, RejectionReason
from config_system.schema import ConfigSchema, FieldSpec, FlagSpec
from config_system.sources import defaults_layer, load_env_layer, load_file_layer


def _schema():
    return ConfigSchema(
        fields=(
            FieldSpec(key="greeting", type="str", default="hi"),
            FieldSpec(key="limit", type="int", required=True),
            FieldSpec(key="nodefault", type="str"),
        ),
        flags=(FlagSpec(name="beta", default=False),),
    )


def test_defaults_layer():
    layer = defaults_layer(_schema())
    assert layer["fields"] == {"greeting": "hi"}
    assert layer["flags"] == {"beta": {"enabled": False}}


def test_file_layer_missing_is_empty(tmp_path):
    layer = load_file_layer(str(tmp_path / "nope.json"))
    assert layer == {"fields": {}, "flags": {}}


def test_file_layer_namespaced_and_flat(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "fields": {"limit": 10},
        "flags": {"beta": {"enabled": True, "percentage": 25}, "plain": True},
        "extra": "flat",
    }))
    layer = load_file_layer(str(p))
    assert layer["fields"]["limit"] == 10
    assert layer["fields"]["extra"] == "flat"
    assert layer["flags"]["beta"] == {"enabled": True, "percentage": 25}
    assert layer["flags"]["plain"] == {"enabled": True}


def test_file_layer_invalid_json_rejected(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(ConfigRejectionError) as ei:
        load_file_layer(str(p))
    assert ei.value.reason is RejectionReason.SOURCE_READ_ERROR


def test_env_layer_parsing():
    env = {
        "APP_FIELD_LIMIT": "42",
        "APP_FIELD_GREETING": "hello",
        "APP_FLAG_BETA": "on",
        "APP_ROLLOUT_BETA": "30",
        "OTHER_IGNORED": "x",
    }
    layer = load_env_layer("APP_", env)
    assert layer["fields"]["limit"] == 42
    assert layer["fields"]["greeting"] == "hello"
    assert layer["flags"]["beta"] == {"enabled": True, "percentage": 30}
    assert "OTHER_IGNORED" not in layer["fields"]
