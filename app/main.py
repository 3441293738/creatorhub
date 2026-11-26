"""CreatorHub ASGI 服务入口：负责中间件、生命周期、静态资源与子路由装配。"""
from __future__ import annotations

import mimetypes

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import runtime
from .api import register_routes
from .local_access import LocalAccessMiddleware
from .logging_setup import RequestLoggingMiddleware
from .services.lifecycle import lifespan

WEB_DIR = runtime.WEB_DIR

# Windows 注册表常被第三方编辑器改写为非标准 Content Type（如 .css -> application/x-css、
# .js -> text/plain），配合 nosniff 会被浏览器拒载；启动时显式锁定标准 Web/媒体 MIME 映射。
_STANDARD_MIME_TYPES: tuple[tuple[str, str], ...] = (
    (".css", "text/css"),
    (".js", "text/javascript"),
    (".mjs", "text/javascript"),
    (".html", "text/html"),
    (".htm", "text/html"),
    (".json", "application/json"),
    (".svg", "image/svg+xml"),
    (".ico", "image/x-icon"),
    (".png", "image/png"),
    (".jpg", "image/jpeg"),
    (".jpeg", "image/jpeg"),
    (".gif", "image/gif"),
    (".webp", "image/webp"),
    (".avif", "image/avif"),
    (".woff", "font/woff"),
    (".woff2", "font/woff2"),
    (".ttf", "font/ttf"),
    (".wasm", "application/wasm"),
    (".txt", "text/plain"),
    (".mp4", "video/mp4"),
    (".webm", "video/webm"),
    (".mov", "video/quicktime"),
    (".mp3", "audio/mpeg"),
    (".m4a", "audio/mp4"),
)


def _ensure_standard_mime_types() -> None:
    for ext, media_type in _STANDARD_MIME_TYPES:
        mimetypes.add_type(media_type, ext, strict=True)


def create_app() -> FastAPI:
    """装配本地 FastAPI 应用实例（导入阶段不启动浏览器或后台协程）。"""
    _ensure_standard_mime_types()
    application = FastAPI(title="CreatorHub", lifespan=lifespan)
    application.add_middleware(LocalAccessMiddleware)
    application.add_middleware(RequestLoggingMiddleware, cfg_getter=lambda: runtime.cfg)
    register_routes(application)
    application.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    return application


app = create_app()
