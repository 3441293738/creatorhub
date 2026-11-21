"""前端单页工作台 HTML 入口与服务健康检查接口。"""
from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app import runtime as app_runtime

router = APIRouter()


# ─────────── 前端 ───────────
@router.get("/", response_class=HTMLResponse)
async def index():
    html = (app_runtime.WEB_DIR / "index.html").read_text(encoding="utf-8")
    if os.environ.get("CREATORHUB_DESKTOP") == "1":
        html = html.replace('data-guide-base="https://3441293738.github.io/creatorhub/guide/"',
                            'data-guide-base="/guide/"')
    # Version all local UI assets, including pre-paint appearance boot and CSS.
    try:
        for asset in ("app.js", "submissions.js", "appearance.js", "appearance.css", "workspace-ui.js", "workbench.js", "workbench.css", "agent.css"):
            ver = int((app_runtime.WEB_DIR / asset).stat().st_mtime)
            html = html.replace(f"/static/{asset}", f"/static/{asset}?v={ver}")
    except Exception:
        pass
    # 首页(含内联 CSS)禁缓存:否则 webview 缓存旧 HTML,改了样式也不生效
    return HTMLResponse(html, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache", "Expires": "0"})


@router.get("/health")
async def health():
    return {"status": "ok"}
