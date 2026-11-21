"""CreatorHub ASGI 服务入口：负责中间件、生命周期、静态资源与子路由装配。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import runtime
from .api import register_routes
from .local_access import LocalAccessMiddleware
from .services.lifecycle import lifespan

WEB_DIR = runtime.WEB_DIR


def create_app() -> FastAPI:
    """装配本地 FastAPI 应用实例（导入阶段不启动浏览器或后台协程）。"""
    application = FastAPI(title="CreatorHub", lifespan=lifespan)
    application.add_middleware(LocalAccessMiddleware)
    register_routes(application)
    application.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    return application


app = create_app()
