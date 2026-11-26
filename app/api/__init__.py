"""FastAPI 业务路由注册表：按领域挂载全部子路由与 AI 助手路由。"""
from __future__ import annotations

from fastapi import FastAPI, Request

from ..agent.api import create_router as create_agent_router
from . import login
from . import contents
from . import workbench
from . import matrix
from . import channels
from . import accounts
from . import progress
from . import task_queue
from . import risk_control
from . import browser_runtimes
from . import account_works
from . import follows
from . import messages
from . import account_actions
from . import account_browser
from . import proxies
from . import settings
from . import diagnostics
from . import share_downloads
from . import platform_tools
from . import collections
from . import monitors
from . import reports
from . import comments
from . import danmaku
from . import publishing
from . import published_notes
from . import comment_rules
from . import community
from . import notifications
from . import pages


async def _agent_create_collection(payload: dict, request: Request):
    return await collections.create_keyword_collection(
        collections.KeywordCollectionIn(**payload), request=request
    )


async def _agent_create_monitor(payload: dict, request: Request):
    return await monitors.add_monitor(
        monitors.TargetIn(**payload), request=request
    )


def register_routes(app: FastAPI) -> None:
    """按业务域挂载全部 HTTP 路由，保持原有 URL 契约与执行语义不变。"""
    app.include_router(login.router)
    app.include_router(contents.router)
    app.include_router(workbench.router)
    app.include_router(matrix.router)
    app.include_router(channels.router)
    app.include_router(accounts.router)
    app.include_router(progress.router)
    app.include_router(task_queue.router)
    app.include_router(risk_control.router)
    app.include_router(browser_runtimes.router)
    app.include_router(account_works.router)
    app.include_router(follows.router)
    app.include_router(messages.router)
    app.include_router(account_actions.router)
    app.include_router(account_browser.router)
    app.include_router(proxies.router)
    app.include_router(settings.router)
    app.include_router(diagnostics.router)
    app.include_router(share_downloads.router)
    app.include_router(platform_tools.router)
    app.include_router(collections.router)
    app.include_router(monitors.router)
    app.include_router(reports.router)
    app.include_router(comments.router)
    app.include_router(danmaku.router)
    app.include_router(publishing.router)
    app.include_router(published_notes.router)
    app.include_router(comment_rules.router)
    app.include_router(community.router)
    app.include_router(notifications.router)
    app.include_router(pages.router)
    app.include_router(
        create_agent_router(
            {
                "create_collection": _agent_create_collection,
                "create_monitor": _agent_create_monitor,
            }
        )
    )
