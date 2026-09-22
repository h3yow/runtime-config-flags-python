"""应用层组装：默认值 < 配置文件 < 环境变量。

配置文件路径由环境变量 APP_CONFIG_FILE 指定，默认 config.json；
该默认文件缺失时按空层处理（仅影响演示便利性；显式指定路径时
缺失仍会被拒绝，见 FileSource）。
"""

from __future__ import annotations

import os
from functools import lru_cache

from app.manager import ConfigManager
from app.service import ConfigService
from app.sources import DefaultsSource, EnvironmentSource, FileSource

# 完整的内置默认值：即便没有配置文件、没有环境变量，服务也能用
# 一套合法的默认配置启动（生产环境应通过文件/环境变量覆盖 secret_token）。
DEFAULT_SETTINGS: dict = {
    "settings": {
        "service_name": "default-service",
        "max_connections": 100,
        "request_timeout_ms": 2000,
        "enable_cache": True,
        "api_base_url": "http://localhost:8000",
        "secret_token": "change-me",
    }
}


@lru_cache(maxsize=1)
def get_config_manager() -> ConfigManager:
    return ConfigManager()


def build_config_service() -> ConfigService:
    config_path = os.environ.get("APP_CONFIG_FILE", "config.json")
    explicit = "APP_CONFIG_FILE" in os.environ
    manager = get_config_manager()
    return ConfigService(
        manager,
        [
            DefaultsSource(DEFAULT_SETTINGS),
            FileSource(config_path, optional=not explicit),
            EnvironmentSource(prefix="APP_", environ=dict(os.environ)),
        ],
    )


@lru_cache(maxsize=1)
def get_config_service() -> ConfigService:
    return build_config_service()
