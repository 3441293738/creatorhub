"""应用启动与优雅关闭生命周期编排（数据库、浏览器、调度引擎、私信与代理通道）。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import asyncio

from fastapi import FastAPI

from app import runtime as app_runtime
from app.account_merge import (
    cleanup_merged_profiles,
    cleanup_orphan_profiles,
    reconcile_platform_accounts,
)
from app.browser import BrowserManager
from app.channels_settings import load_persisted_channels_settings
from app.db import get_session, init_db
from app.engine import MonitorEngine
from app.engine_settings import load_persisted_engine_settings
from app.event_center import dispatch_pending
from app.platforms.channels import recover_stale_system_proxy
from app.profiles import migrate_identities, seed_proxy_pool
from app.risk_admin import load_persisted_risk_settings
from app.services import browser_runtimes as browser_runtimes_service
from app.services import login_sessions as login_sessions_service
from app.services import metadata as metadata_service
from app.services import share_downloads as share_downloads_service
from app.services import watch_records as watch_records_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    
    init_db(app_runtime.cfg.db_path)
    from app.agent.runtime import recover_interrupted, shutdown as shutdown_agent
    recover_interrupted()
    try:
        with get_session() as session:
            merged_accounts = reconcile_platform_accounts(session)
        if merged_accounts:
            cleanup_merged_profiles(merged_accounts, app_runtime.cfg.engine.profiles_dir)
            print(
                f"[startup] 已自动合并 {len(merged_accounts)} 条同平台重复登录记录")
        with get_session() as session:
            removed_orphans = cleanup_orphan_profiles(
                session, app_runtime.cfg.engine.profiles_dir
            )
        if removed_orphans:
            print(f"[startup] 已清理 {removed_orphans} 个孤立/残留浏览器目录")
    except Exception as e:
        print(f"[startup] 账号去重或残留目录清理失败(不影响启动): {e!r}")
    if load_persisted_risk_settings(app_runtime.cfg):
        print("[startup] 已加载风控中心保存的运行时规则")
    if load_persisted_engine_settings(app_runtime.cfg):
        print("[startup] 已加载设置页保存的采集与运行配置")
    app_runtime.channels_bridge.configure_data_dir(Path(app_runtime.cfg.db_path).resolve().parent)
    channels_proxy_dir = Path(app_runtime.cfg.db_path).resolve().parent
    app_runtime.channels_proxy.configure_data_dir(channels_proxy_dir)
    try:
        proxy_recovery = recover_stale_system_proxy(channels_proxy_dir)
        if proxy_recovery.get("recovered"):
            print("[startup] 已自动恢复上次未正常退出残留的 Windows 系统代理")
    except Exception as e:
        print(f"[startup] 清理残留系统代理失败（不影响启动）: {e!r}")
    if load_persisted_channels_settings(app_runtime.cfg):
        print("[startup] 已加载视频号微信通道配置")
    try:
        auto_proxy = await metadata_service.auto_start_persisted_channels_proxy(app_runtime.cfg, app_runtime.channels_proxy)
        if auto_proxy.get("started"):
            bound = auto_proxy.get("proxy", {}).get("proxy_port") or app_runtime.cfg.engine.channels_proxy_port
            print(f"[startup] 已自动恢复视频号微信本地代理 (127.0.0.1:{bound})")
    except Exception as e:
        print(f"[startup] 自动恢复视频号微信代理跳过（不影响启动）: {e!r}")
    try:
        repaired = watch_records_service._backfill_danmaku_records()
        if repaired:
            print(f"[startup] 已补齐 {repaired} 条弹幕的时间/用户字段")
    except Exception as e:
        print(f"[startup] 弹幕存量字段补齐失败（不影响启动）: {e!r}")
    try:
        restored = share_downloads_service._backfill_share_download_history()
        if restored:
            print(f"[startup] 已从本地下载目录补录 {restored} 条链接下载历史")
    except Exception as e:
        print(f"[startup] 链接下载历史补录失败（不影响启动）: {e!r}")
    # config.yaml 里配的 proxies 导入数据库代理池(之后统一在页面管理)
    try:
        seeded = seed_proxy_pool(app_runtime.cfg)
        if seeded:
            print(f"[startup] 已从 config.yaml 导入 {seeded} 条代理到代理池")
    except Exception as e:
        print(f"[startup] 代理池导入失败(不影响启动): {e!r}")
    # 存量账号补齐设备/网络画像(profile_dir / UA / 指纹 / 代理),防多账号关联
    try:
        n = migrate_identities(app_runtime.cfg)
        if n:
            print(f"[startup] 已为 {n} 个存量账号补齐画像(profile/UA/指纹/代理)")
    except Exception as e:
        print(f"[startup] 账号画像迁移失败(不影响启动): {e!r}")
    try:
        runtime_specs = browser_runtimes_service._seed_browser_runtimes()
    except Exception as e:
        print(f"[startup] 浏览器内核目录导入失败(不影响启动): {e!r}")
        runtime_specs = []
    default_runtime_id = next(
        (item["runtime_id"] for item in runtime_specs
         if item.get("is_default") and item.get("enabled")), "")
    app_runtime.browser = BrowserManager(
        app_runtime.cfg.engine.user_agent, app_runtime.cfg.engine.profiles_dir,
        app_runtime.cfg.engine.max_live_contexts, native_ua_callback=login_sessions_service._persist_native_ua,
        xhs_browser_mode=app_runtime.cfg.engine.xhs_browser_mode,
        xhs_cdp_idle_seconds=app_runtime.cfg.engine.xhs_cdp_idle_seconds,
        resident_sessions=app_runtime.cfg.engine.resident_browser_sessions,
        session_idle_seconds=app_runtime.cfg.engine.browser_session_idle_seconds,
        native_write_gate_enabled=app_runtime.cfg.engine.native_write_gate_enabled,
        native_write_require_system_chrome=app_runtime.cfg.engine.native_write_require_system_chrome,
        native_write_require_verified_proxy=app_runtime.cfg.engine.native_write_require_verified_proxy,
        native_write_proxy_max_age_seconds=app_runtime.cfg.engine.native_write_proxy_max_age_seconds,
        browser_exit_probe_url=app_runtime.cfg.engine.browser_exit_probe_url,
        browser_backend=app_runtime.cfg.engine.browser_backend,
        fingerprint_chromium_path=(
            "" if runtime_specs else app_runtime.cfg.engine.fingerprint_chromium_path),
        fingerprint_chromium_allow_headless=(
            app_runtime.cfg.engine.fingerprint_chromium_allow_headless),
        fingerprint_chromium_platform=(
            app_runtime.cfg.engine.fingerprint_chromium_platform),
        fingerprint_chromium_runtimes=runtime_specs,
        fingerprint_default_runtime_id=default_runtime_id,
        douyin_read_session_callback=login_sessions_service._persist_douyin_read_session)
    await app_runtime.browser.start()
    app_runtime.engine = MonitorEngine(app_runtime.cfg, app_runtime.browser)
    startup_now = datetime.utcnow()
    pruned_risk_events = app_runtime.engine._prune_risk_events_if_due(startup_now)
    if pruned_risk_events:
        print(f"[startup] 已清理 {pruned_risk_events} 条过期风控事件")
    recovered = app_runtime.engine.recover_interrupted_tasks()
    if recovered:
        print(f"[startup] 已恢复 {recovered} 条中断的写任务")
    app_runtime.engine.start()
    from app.engine.im_receiver import ImReceiverManager
    app_runtime.im_receiver = ImReceiverManager(app_runtime.browser)
    publisher = getattr(app_runtime.im_receiver, "publish", None)
    if callable(publisher):
        app_runtime.engine.set_dm_event_sink(publisher)
    async def webhook_delivery_loop():
        while True:
            try:
                await dispatch_pending(20)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[webhook] 事件投递循环异常: {exc!r}")
            await asyncio.sleep(15)
    app_runtime._webhook_delivery_task = asyncio.create_task(webhook_delivery_loop())
    yield
    await shutdown_agent()
    if app_runtime._webhook_delivery_task:
        app_runtime._webhook_delivery_task.cancel()
        await asyncio.gather(app_runtime._webhook_delivery_task, return_exceptions=True)
        app_runtime._webhook_delivery_task = None
    follow_tasks = [task for task in app_runtime._follow_sync_tasks.values()
                    if not task.done()]
    for task in follow_tasks:
        task.cancel()
    if follow_tasks:
        await asyncio.gather(*follow_tasks, return_exceptions=True)
    if app_runtime.im_receiver:
        await app_runtime.im_receiver.stop_all()
    if app_runtime.engine:
        await app_runtime.engine.stop()
    if app_runtime.browser:
        await app_runtime.browser.stop()
    await app_runtime.channels_bridge.stop()
    await app_runtime.channels_proxy.stop()
