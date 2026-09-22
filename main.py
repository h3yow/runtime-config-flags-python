import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router as config_router
from app.bootstrap import get_config_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即加载初始版本；配置非法时拒绝启动，而不是带着错误配置运行
    outcome = get_config_service().load(initial=True)
    logger.info("config subsystem ready, version=%s", outcome.snapshot.version)
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(config_router)


@app.get("/")
def read_root():
    return {"Hello": "World"}
