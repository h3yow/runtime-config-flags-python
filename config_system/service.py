"""FastAPI 装配：在既有 Hello World 服务上增加配置/开关接口。"""

from __future__ import annotations

from fastapi import FastAPI, Request


def create_app(manager=None) -> FastAPI:
    raise NotImplementedError
