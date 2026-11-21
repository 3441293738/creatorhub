"""进程级应用运行时状态容器；供各路由与服务模块共享，避免反向导入 app.main。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
import asyncio
import threading

from app.browser import BrowserManager
from app.config import load_config
from app.engine import MonitorEngine
from app.platforms.channels import ChannelsBridgeManager, ChannelsProxyManager, set_active_proxy_manager


cfg = load_config()
browser: BrowserManager | None = None
engine: MonitorEngine | None = None
im_receiver = None      # ImReceiverManager(私信实时接收)
channels_bridge = ChannelsBridgeManager(Path(cfg.db_path).resolve().parent)
channels_proxy = ChannelsProxyManager(Path(cfg.db_path).resolve().parent)
set_active_proxy_manager(channels_proxy)
login_tasks: Dict[str, dict] = {}
# 用户手动打开的账号浏览器窗口(account_id -> BrowserContext),留引用防 GC、便于复用/清理
open_browsers: Dict[int, Any] = {}
_file_manager_lock = threading.Lock()
_share_download_sem = asyncio.Semaphore(2)
_follow_sync_jobs: Dict[str, dict] = {}
_follow_sync_tasks: Dict[str, asyncio.Task] = {}
_webhook_delivery_task: asyncio.Task | None = None
WEB_DIR = Path(__file__).parent / "web"
