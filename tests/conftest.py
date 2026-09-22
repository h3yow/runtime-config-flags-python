import json

import pytest

from config_system.schema import ConfigSchema, FieldSpec, FlagSpec


@pytest.fixture
def schema():
    return ConfigSchema(
        fields=(
            FieldSpec(key="greeting", type="str", default="hello"),
            FieldSpec(key="limit", type="int", required=True, min_value=1, max_value=1000),
            FieldSpec(key="api_key", type="secret", required=True),
        ),
        flags=(FlagSpec(name="beta"), FlagSpec(name="new_ui")),
    )


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.json"

    def write(data):
        path.write_text(json.dumps(data))
        return str(path)

    write({"fields": {"limit": 10, "api_key": "key-v1"}})
    return write
