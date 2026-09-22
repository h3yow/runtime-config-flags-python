"""分层合并优先级、来源溯源与校验拒绝原因测试。"""

from __future__ import annotations

import logging

import pytest

from app.errors import ConfigValidationError
from app.merge import merge_layers
from app.sources import DefaultsSource, EnvironmentSource, FileSource
from app.service import ConfigService
from app.manager import ConfigManager
from app.validation import validate

logger = logging.getLogger("tests")

SETTINGS = {
    "service_name": "svc",
    "max_connections": 100,
    "request_timeout_ms": 2000,
    "api_base_url": "https://api",
    "secret_token": "secret-value",
}


def test_layer_priority_defaults_file_env(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(
        '{"settings": {"max_connections": 50, "request_timeout_ms": 2000,'
        ' "api_base_url": "https://f", "secret_token": "k", "service_name": "file-svc"}}'
    )
    service = ConfigService(
        ConfigManager(),
        [
            DefaultsSource({"settings": {"max_connections": 10, "enable_cache": False}}),
            FileSource(str(cfg)),
            EnvironmentSource(
                environ={"APP_SETTINGS__MAX_CONNECTIONS": "200"}
            ),
        ],
    )
    outcome = service.load(initial=True)
    s = outcome.snapshot.settings
    # env 覆盖 file 覆盖 defaults
    assert s.max_connections == 200
    assert s.service_name == "file-svc"
    assert s.enable_cache is False
    # 溯源可解释
    assert outcome.provenance["settings.max_connections"] == "env"
    assert outcome.provenance["settings.service_name"] == "file"
    assert outcome.provenance["settings.enable_cache"] == "defaults"
    logger.info("layer merge published version=%s provenance=%s",
                outcome.snapshot.version, outcome.provenance)


def test_validation_failure_reasons_are_distinguishable():
    bad_cases = [
        ({"settings": {"service_name": "svc"}}, "MISSING_REQUIRED"),
        ({"settings": dict(SETTINGS, max_connections="100")}, "TYPE_MISMATCH"),
        ({"settings": dict(SETTINGS, max_connections=999999)}, "INVALID_VALUE"),
        ({"settings": dict(SETTINGS, service_name="   ")}, "INVALID_VALUE"),
        ({"settings": dict(SETTINGS, max_connections=True)}, "TYPE_MISMATCH"),
        ({"settings": dict(SETTINGS),
          "flags": {"f": {"enabled": 1}}}, "TYPE_MISMATCH"),
        ({"settings": dict(SETTINGS),
          "flags": {"f": {"enabled": True, "rollout": {"percentage": 150}}}},
         "INVALID_VALUE"),
    ]
    for raw, expected_prefix in bad_cases:
        with pytest.raises(ConfigValidationError) as exc_info:
            validate(raw, version=1)
        prefixes = {r.split(":", 1)[0] for r in exc_info.value.reasons}
        logger.info("reject version=1 reasons=%s", exc_info.value.reasons)
        assert expected_prefix in prefixes, (raw, exc_info.value.reasons)


def test_multiple_errors_collected_at_once():
    # 多个问题一次报全，而非遇到第一个就静默回退
    with pytest.raises(ConfigValidationError) as exc_info:
        validate({"settings": {"max_connections": "x"}}, version=3)
    reasons = exc_info.value.reasons
    logger.info("collect version=3 reasons=%s", reasons)
    assert any("MISSING_REQUIRED" in r for r in reasons)
    assert any("TYPE_MISMATCH" in r for r in reasons)
    assert len(reasons) >= 3


def test_valid_snapshot_is_immutable():
    snap = validate({"settings": SETTINGS}, version=1)
    with pytest.raises(Exception):
        snap.version = 2  # type: ignore[misc]
    with pytest.raises(Exception):
        snap.settings.max_connections = 1  # type: ignore[misc]


def test_source_file_missing_and_bad_json_are_source_errors(tmp_path):
    from app.errors import ConfigSourceError

    with pytest.raises(ConfigSourceError) as e1:
        FileSource(str(tmp_path / "nope.json")).load()
    assert "CONFIG_FILE_NOT_FOUND" in str(e1.value)

    bad = tmp_path / "bad.json"
    bad.write_text("{broken")
    with pytest.raises(ConfigSourceError) as e2:
        FileSource(str(bad)).load()
    assert "CONFIG_FILE_INVALID_JSON" in str(e2.value)
