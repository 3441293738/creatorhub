"""应用启动与优雅关闭生命周期编排（日志、数据库、浏览器、调度引擎、私信与代理通道）。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import asyncio
import logging

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
from app.logging_setup import configure_logging, shutdown_logging
from app.progress_hub import progress_hub
from app.platforms.channels import recover_stale_system_proxy
from app.profiles import migrate_identities, seed_proxy_pool
from app.risk_admin import load_persisted_risk_settings
from app.services import browser_runtimes as browser_runtimes_service
from app.services import login_sessions as login_sessions_service
from app.services import metadata as metadata_service
from app.services import share_downloads as share_downloads_service
from app.services import watch_records as watch_records_service

log = logging.getLogger("creatorhub.startup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log_dir = configure_logging(app_runtime.cfg)
    startup_notes: list[str] = []
    loaded_overrides: list[str] = []

    try:
        progress_hub.bind_loop(asyncio.get_running_loop())
        init_db(app_runtime.cfg.db_path)
        from app.agent.runtime import recover_interrupted, shutdown as shutdown_agent
        recover_interrupted()

        try:
            with get_session() as session:
                merged_accounts = reconcile_platform_accounts(session)
            if merged_accounts:
                cleanup_merged_profiles(merged_accounts, app_runtime.cfg.engine.profiles_dir)
                startup_notes.append(f"合并重复账号={len(merged_accounts)}")
            with get_session() as session:
                removed_orphans = cleanup_orphan_profiles(
                    session, app_runtime.cfg.engine.profiles_dir
                )
            if removed_orphans:
                startup_notes.append(f"清理残留目录={removed_orphans}")
        except Exception as e:
            log.warning("账号去重或残留目录清理失败(不影响启动): %r", e)

        if load_persisted_engine_settings(app_runtime.cfg):
            loaded_overrides.append("engine")
        if load_persisted_risk_settings(app_runtime.cfg):
            loaded_overrides.append("risk")

        app_runtime.channels_bridge.configure_data_dir(Path(app_runtime.cfg.db_path).resolve().parent)
        channels_proxy_dir = Path(app_runtime.cfg.db_path).resolve().parent
        app_runtime.channels_proxy.configure_data_dir(channels_proxy_dir)
        try:
            proxy_recovery = recover_stale_system_proxy(channels_proxy_dir)
            if proxy_recovery.get("recovered"):
                startup_notes.append("恢复残留系统代理")
        except Exception as e:
            log.warning("清理残留系统代理失败(不影响启动): %r", e)

        if load_persisted_channels_settings(app_runtime.cfg):
            loaded_overrides.append("channels")

        try:
            auto_proxy = await metadata_service.auto_start_persisted_channels_proxy(
                app_runtime.cfg, app_runtime.channels_proxy
            )
            if auto_proxy.get("started"):
                bound = auto_proxy.get("proxy", {}).get("proxy_port") or app_runtime.cfg.engine.channels_proxy_port
                startup_notes.append(f"视频号本地代理=127.0.0.1:{bound}")
        except Exception as e:
            log.warning("自动恢复视频号微信代理跳过(不影响启动): %r", e)

        try:
            repaired = watch_records_service._backfill_danmaku_records()
            if repaired:
                startup_notes.append(f"补齐弹幕={repaired}")
        except Exception as e:
            log.warning("弹幕存量字段补齐失败(不影响启动): %r", e)

        try:
            restored = share_downloads_service._backfill_share_download_history()
            if restored:
                startup_notes.append(f"补录下载历史={restored}")
        except Exception as e:
            log.warning("链接下载历史补录失败(不影响启动): %r", e)

        try:
            seeded = seed_proxy_pool(app_runtime.cfg)
            if seeded:
                startup_notes.append(f"导入代理池={seeded}")
        except Exception as e:
            log.warning("代理池导入失败(不影响启动): %r", e)

        try:
            migrated = migrate_identities(app_runtime.cfg)
            if migrated:
                startup_notes.append(f"补齐账号画像={migrated}")
        except Exception as e:
            log.warning("账号画像迁移失败(不影响启动): %r", e)

        try:
            runtime_specs = browser_runtimes_service._seed_browser_runtimes()
        except Exception as e:
            log.warning("浏览器内核目录导入失败(不影响启动): %r", e)
            runtime_specs = []

        default_runtime_id = next(
            (item["runtime_id"] for item in runtime_specs
             if item.get("is_default") and item.get("enabled")), ""
        )
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
            douyin_read_session_callback=login_sessions_service._persist_douyin_read_session,
        )
        await app_runtime.browser.start()
        app_runtime.engine = MonitorEngine(app_runtime.cfg, app_runtime.browser)
        startup_now = datetime.utcnow()
        pruned_risk_events = app_runtime.engine._prune_risk_events_if_due(startup_now)
        if pruned_risk_events:
            startup_notes.append(f"清理过期风控事件={pruned_risk_events}")
        recovered = app_runtime.engine.recover_interrupted_tasks()
        if recovered:
            log.warning("启动时检测到 %d 条上次中断的写/采集任务，已按安全边界标记待核对或待恢复", recovered)
        app_runtime.engine.start()

        from app.engine.im_receiver import ImReceiverManager
        app_runtime.im_receiver = ImReceiverManager(app_runtime.browser)
        publisher = getattr(app_runtime.im_receiver, "publish", None)
        if callable(publisher):
            app_runtime.engine.set_dm_event_sink(publisher)

        webhook_log = logging.getLogger("creatorhub.webhook")

        async def webhook_delivery_loop():
            while True:
                try:
                    await dispatch_pending(20)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    webhook_log.warning("事件投递循环异常: %r", exc)
                await asyncio.sleep(15)

        app_runtime._webhook_delivery_task = asyncio.create_task(webhook_delivery_loop())

        summary_parts = [
            f"backend={app_runtime.cfg.engine.browser_backend}",
            f"runtimes={len(runtime_specs)}",
        ]
        if loaded_overrides:
            summary_parts.append("persisted=" + ",".join(loaded_overrides))
        if startup_notes:
            summary_parts.append("actions=" + ";".join(startup_notes))
        log.info("服务初始化就绪 (%s) | logs=%s", " | ".join(summary_parts), log_dir)

        yield
    except Exception:
        log.exception("服务生命周期异常中止")
        raise
    finally:
        log.info("正在优雅停止本地服务与浏览器会话…")
        try:
            from app.agent.runtime import shutdown as shutdown_agent
            await shutdown_agent()
        except Exception:
            pass
        if app_runtime._webhook_delivery_task:
            app_runtime._webhook_delivery_task.cancel()
            await asyncio.gather(app_runtime._webhook_delivery_task, return_exceptions=True)
            app_runtime._webhook_delivery_task = None
        follow_tasks = [task for task in app_runtime._follow_sync_tasks.values()
                        if not task.done()]
        extra_tasks = [
            task
            for pool in (
                app_runtime._share_download_tasks.values(),
                app_runtime._matrix_batch_tasks.values(),
                list(app_runtime._background_tasks),
            )
            for task in pool
            if not task.done()
        ]
        all_bg_tasks = [*follow_tasks, *extra_tasks]
        for task in all_bg_tasks:
            task.cancel()
        if all_bg_tasks:
            await asyncio.gather(*all_bg_tasks, return_exceptions=True)
        if app_runtime.im_receiver:
            await app_runtime.im_receiver.stop_all()
        if app_runtime.engine:
            await app_runtime.engine.stop()
        if app_runtime.browser:
            await app_runtime.browser.stop()
        await app_runtime.channels_bridge.stop()
        await app_runtime.channels_proxy.stop()
        progress_hub.shutdown()
        log.info("服务已安全退出")
        shutdown_logging()
