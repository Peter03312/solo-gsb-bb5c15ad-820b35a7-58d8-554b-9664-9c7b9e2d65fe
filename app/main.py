"""FastAPI 应用入口。

启动时自动建表并写入第 0 号目录修订（空目录），保证开箱即用。
生产环境也可改用 Alembic 迁移，本服务为离线实验室内网场景保持自包含。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import repository
from .config import settings
from .database import Base, SessionLocal, engine
from .models import CatalogRevision
from .routers import router


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if repository.latest_revision(db) == 0:
            db.add(CatalogRevision(kind="init", plugin=None, version=None,
                                   note="初始空目录修订"))
            db.commit()
    finally:
        db.close()
    os.makedirs(settings.package_dir, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 等待 compose 中 depends_on healthy 后，数据库已就绪；重试若干次以防时序
    import time
    last_err = None
    for i in range(30):
        try:
            init_db()
            last_err = None
            break
        except Exception as e:  # 数据库尚未就绪
            last_err = e
            time.sleep(1)
    if last_err is not None:
        raise last_err
    yield


app = FastAPI(
    title="离线实验室插件发布与安装方案服务",
    version="1.0.0",
    description=(
        "在不可联网的实验室内网中发布插件版本、按目录修订求解满足全部传递"
        "依赖的完整安装方案。详见 README.md。"
    ),
    lifespan=lifespan,
)

app.include_router(router, prefix="/api/v1")


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "plugin-catalog",
        "docs": "/docs",
        "api": "/api/v1",
    }
