"""交互式登录会话状态跟踪、浏览器窗口租约与原生会话持久化辅助。"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
import hashlib
import json

from fastapi import HTTPException
from sqlmodel import select

from app import runtime as app_runtime
from app.browser.storage_state import capture_context_storage_state, merge_state_origins
from app.db import get_session
from app.models import AccountActionTask, CommentTask, DouyinAccount, PublishTask
from app.windowing import CHROMIUM_WINDOW_CLASSES, bring_window_to_front, capture_window_snapshot


_ACTIVE_LOGIN_STATUSES = {"opening", "waiting", "verification", "persisted"}


def _login_task_state(*, status: str, platform: str,
                      creator: bool, account_id: int | None,
                      **details) -> dict:
    """Build a login task update without dropping its conflict metadata."""
    return {
        "status": status,
        "platform": platform,
        "creator": bool(creator),
        "account_id": account_id,
        **details,
    }


def _active_login_task() -> tuple[str, dict] | None:
    """Return the most recently started interactive login that still owns a window."""
    for task_id, state in reversed(list(app_runtime.login_tasks.items())):
        if str(state.get("status") or "") in _ACTIVE_LOGIN_STATUSES:
            return task_id, state
    return None


def _schedule_dead_lease_reap(account_id: int, lease: "_OpenBrowserLease") -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if not loop.is_closed():
        loop.create_task(_release_open_browser(account_id, lease))


def _lease_is_alive(lease) -> bool:
    if lease is None:
        return False
    checker = getattr(lease, "is_alive", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return bool(getattr(lease, "active", True))


def _active_open_browser_account_ids() -> list[int]:
    alive: list[int] = []
    for account_id, lease in list(app_runtime.open_browsers.items()):
        if _lease_is_alive(lease):
            alive.append(account_id)
        else:
            _schedule_dead_lease_reap(account_id, lease)
    return alive


async def has_active_open_browser(account_id: int | None) -> bool:
    """Return True if this account owns a live manual browser window; reap stale leases."""
    if not account_id:
        return False
    lease = app_runtime.open_browsers.get(account_id)
    if lease is None:
        return False
    if _lease_is_alive(lease):
        return True
    await _release_open_browser(account_id, lease)
    return False


async def close_open_browser_for_account(
    account_id: int | None,
    *,
    nudge: bool = True,
) -> bool:
    """Persist cookies, close the manual browser window, and release its account lock."""
    if not account_id:
        return False
    lease = app_runtime.open_browsers.pop(account_id, None)
    closed = False
    if lease is not None:
        try:
            await lease.close()
            closed = True
        except Exception:
            pass
    if app_runtime.browser is not None:
        with suppress(Exception):
            await app_runtime.browser.close_context(account_id)
    _wake_open_browser_blocked_tasks(account_id, nudge=nudge)
    return closed


def _login_scope_label(platform: str, creator: bool) -> str:
    label = {
        "xhs": "小红书",
        "rednote": "海外小红书",
        "kuaishou": "快手",
        "shipinhao": "视频号",
        "tiktok": "TikTok",
        "bilibili": "哔哩哔哩",
        "instagram": "Instagram",
        "twitter": "Twitter",
        "facebook": "Facebook",
        "youtube": "YouTube",
        "douyin": "抖音",
    }.get(platform, platform or "平台")
    return f"{label}创作者" if creator else label


async def _reuse_or_reject_interactive_login(
        platform: str, creator: bool, account_id: int | None = None,
) -> dict | None:
    """Refocus duplicate login clicks; reject a different login instead of queueing forever."""
    open_account_ids = _active_open_browser_account_ids()
    if open_account_ids:
        account_text = "、".join(str(value) for value in open_account_ids[:4])
        raise HTTPException(
            409,
            "已有账号浏览器窗口打开"
            f"（账号 {account_text}），扫码登录会被可见窗口队列阻塞。"
            "请先关闭已打开的指纹浏览器窗口，再点击添加账号。",
        )

    active = _active_login_task()
    if active is None:
        return None
    task_id, state = active
    same_login = (
        str(state.get("platform") or "") == platform
        and bool(state.get("creator")) == bool(creator)
        and state.get("account_id") == account_id
    )
    if not same_login:
        active_label = _login_scope_label(
            str(state.get("platform") or ""), bool(state.get("creator")))
        raise HTTPException(
            409,
            f"已有{active_label}扫码登录窗口正在等待操作。"
            "请先在该窗口完成登录或将其关闭，再启动新的登录。",
        )

    # A repeated click usually means the existing Chromium window is behind the
    # dashboard. Raise it instead of creating a second task that waits forever.
    snapshot = capture_window_snapshot(CHROMIUM_WINDOW_CLASSES)
    title_hint = _login_scope_label(platform, creator).replace("创作者", "")
    await asyncio.to_thread(
        bring_window_to_front, snapshot, CHROMIUM_WINDOW_CLASSES,
        title_hint, 1.0,
    )
    return {
        "task_id": task_id,
        "status": state.get("status", "waiting"),
        "reused": True,
        "hint": "已有扫码登录窗口，已尝试将它切换到前台",
    }


class _OpenBrowserLease:
    """Keep the unified account/network guard until a headed context closes."""

    def __init__(self, context, guard, close_callback=None, page=None,
                 *, account_id: int | None = None, open_scope: str = "read",
                 platform: str = ""):
        self.context = context
        self.guard = guard
        self.close_callback = close_callback
        self.page = page
        self.account_id = account_id
        self.open_scope = open_scope
        self.platform = str(platform or "").strip().lower()
        self._released = False
        self._closed = False
        self._release_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._persist_lock = asyncio.Lock()
        self._last_cookie_digest = ""
        self._sync_task: asyncio.Task | None = None
        self._debounce_task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return not self._released and not self._closed

    def is_alive(self) -> bool:
        """Verify the underlying Playwright/Patchright context still has a live page."""
        if not self.active or self.context is None:
            return False
        try:
            browser = getattr(self.context, "browser", None)
            if browser is not None:
                is_connected = getattr(browser, "is_connected", None)
                if callable(is_connected) and not is_connected():
                    return False
            pages = getattr(self.context, "pages", None)
            if pages is not None:
                page_list = list(pages)
                if not page_list:
                    return False
                if all(bool(getattr(p, "is_closed", lambda: False)()) for p in page_list):
                    return False
        except Exception:
            return False
        return True

    def start_state_sync(self, interval_seconds: float = 8.0) -> None:
        """Periodically persist changed cookies while the manual window is open.

        If the user solves an in-page captcha and then closes Chromium with the
        window X button, ``ctx.on('close')`` runs after the context is already
        torn down and ``storage_state()`` raises TargetClosedError. Syncing on
        cookie changes while open ensures verified cookies still land in DB.
        """
        if self._sync_task is not None and not self._sync_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if loop.is_closed():
            return
        step = max(3.0, float(interval_seconds))

        async def _loop() -> None:
            try:
                while self.active and self.is_alive():
                    await asyncio.sleep(step)
                    if not self.active or not self.is_alive():
                        break
                    await self._persist_context_state(force=False)
            except asyncio.CancelledError:
                pass

        self._sync_task = loop.create_task(_loop())

    def schedule_persist(self, delay: float = 1.5) -> None:
        """Debounced cookie persistence triggered by manual window navigation."""
        if not self.active:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if loop.is_closed():
            return
        if self._debounce_task is not None and not self._debounce_task.done():
            self._debounce_task.cancel()

        async def _run() -> None:
            try:
                await asyncio.sleep(max(0.2, float(delay)))
                if self.active and self.is_alive():
                    await self._persist_context_state(force=False)
            except asyncio.CancelledError:
                pass

        self._debounce_task = loop.create_task(_run())

    @staticmethod
    def _has_douyin_auth_cookies(state_obj: dict) -> bool:
        auth_names = {"sessionid", "sessionid_ss", "sid_tt"}
        return any(
            isinstance(c, dict) and str(c.get("name") or "") in auth_names
            for c in (state_obj.get("cookies") or [])
        )

    async def _persist_context_state(self, *, force: bool = True) -> bool:
        if not self.account_id or self.context is None:
            return False
        async with self._persist_lock:
            if not force and not self.active:
                return False
            try:
                if not self.platform:
                    with get_session() as session:
                        account = session.get(DouyinAccount, self.account_id)
                        if account is None:
                            return False
                        self.platform = str(account.platform or "").strip().lower()
                live_state = await capture_context_storage_state(
                    self.context,
                    platform=self.platform,
                    timeout=5.0,
                    filter_cookies_by_platform=(self.platform == "rednote"),
                )
                if not isinstance(live_state, dict) or not live_state.get("cookies"):
                    return False
                live_digest = hashlib.sha256(
                    json.dumps(live_state, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
                if not force and live_digest == self._last_cookie_digest:
                    return False
                with get_session() as session:
                    account = session.get(DouyinAccount, self.account_id)
                    if account is None:
                        return False
                    platform = str(account.platform or self.platform or "").strip().lower()
                    self.platform = platform
                    is_creator_scope = (
                        self.open_scope == "creator" and platform in {"xhs", "rednote"}
                    )
                    prev_raw = (
                        account.creator_storage_state if is_creator_scope else account.storage_state
                    ) or ""
                    state_obj = {
                        "cookies": live_state["cookies"],
                        "origins": merge_state_origins(
                            prev_raw, live_state.get("origins"), platform=platform
                        ),
                    }
                    if platform == "douyin" and account.storage_state:
                        try:
                            prev_state = json.loads(account.storage_state)
                        except Exception:
                            prev_state = {}
                        if (
                            isinstance(prev_state, dict)
                            and self._has_douyin_auth_cookies(prev_state)
                            and not self._has_douyin_auth_cookies(state_obj)
                        ):
                            return False
                    state_json = json.dumps(state_obj, ensure_ascii=False)
                    now_utc = datetime.utcnow()
                    if is_creator_scope:
                        account.creator_storage_state = state_json
                    else:
                        account.storage_state = state_json
                        if platform in {"kuaishou", "shipinhao", "bilibili", "instagram", "twitter", "facebook", "youtube"}:
                            account.creator_storage_state = state_json
                    account.last_keepalive_at = now_utc
                    account.last_cookie_refresh_at = now_utc
                    account.keepalive_note = "已从账号浏览器窗口同步最新登录态"
                    session.add(account)
                    session.commit()
                self._last_cookie_digest = live_digest
                return True
            except Exception:
                # 窗口若已被用户直接点 X 关闭，cookies() 可能抛 TargetClosedError；静默跳过即可
                return False

    async def release(self) -> None:
        async with self._release_lock:
            if self._released:
                return
            self._released = True
            with suppress(Exception):
                await self.guard.__aexit__(None, None, None)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            for bg_task in (self._debounce_task, self._sync_task):
                if bg_task is not None and not bg_task.done():
                    bg_task.cancel()
            try:
                await self._persist_context_state(force=True)
                with suppress(Exception):
                    if self.close_callback is not None:
                        await asyncio.wait_for(self.close_callback(), timeout=10.0)
                    elif self.context is not None:
                        await asyncio.wait_for(self.context.close(), timeout=10.0)
            finally:
                await self.release()


def _wake_open_browser_blocked_tasks(account_id: int, *, nudge: bool = True) -> None:
    """Clear open_browser deferral markers once the manual window closes and optionally nudge publisher."""
    if not account_id:
        return
    woken_publish = False
    try:
        with get_session() as session:
            changed = False
            for model in (PublishTask, CommentTask, AccountActionTask):
                rows = session.exec(
                    select(model).where(
                        model.account_id == account_id,
                        model.status == "pending",
                        model.blocked_signal == "open_browser",
                    )
                ).all()
                for row in rows:
                    row.blocked_reason = ""
                    row.blocked_signal = ""
                    row.blocked_operation = ""
                    row.blocked_at = None
                    row.next_allowed_at = None
                    if row.error and "手动浏览器" in row.error:
                        row.error = ""
                    session.add(row)
                    changed = True
                    if model is PublishTask:
                        woken_publish = True
            if changed:
                session.commit()
    except Exception:
        pass
    if nudge and woken_publish and app_runtime.engine is not None:
        nudge_fn = getattr(app_runtime.engine, "nudge_publish_queue", None)
        if callable(nudge_fn):
            with suppress(Exception):
                nudge_fn()


async def _release_open_browser(account_id: int, lease: _OpenBrowserLease) -> None:
    try:
        await lease.close()
    finally:
        if app_runtime.open_browsers.get(account_id) is lease:
            app_runtime.open_browsers.pop(account_id, None)
        _wake_open_browser_blocked_tasks(account_id)


def _persist_native_ua(account_id: int, ua: str) -> None:
    """Persist the UA observed from an account's native Chromium context."""
    value = str(ua or "").strip()
    if not account_id or not value:
        return
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if account and account.identity_mode == "native" and account.ua != value:
            account.ua = value
            session.add(account)
            session.commit()


def _persist_douyin_read_session(identity, profile: dict, state: dict, recipe: dict) -> bool:
    """Save current browser cookies only after matching authoritative self data.

    A manually completed verification used to update the Chromium profile but
    leave the direct API using the old database snapshot indefinitely.
    """
    from app.models import AppSetting
    from app.platforms.douyin.web_session import context_binding, context_key

    with get_session() as session:
        account = session.get(DouyinAccount, identity.account_id)
        if not account or account.platform != "douyin" or account.profile_dir != identity.profile_dir:
            return False
        comparisons = [str(profile[field]) == str(getattr(account, field))
                       for field in ("uid", "sec_uid")
                       if profile.get(field) and getattr(account, field, "")]
        if not comparisons or not all(comparisons):
            return False
        ua = str(recipe.get("ua") or identity.ua or account.ua)
        binding = context_binding(account, state, ua)
        if not binding:
            return False
        now_utc = datetime.utcnow()
        merged_state = dict(state)
        merged_state["origins"] = merge_state_origins(
            account.storage_state, state.get("origins"), platform="douyin"
        )
        account.storage_state = json.dumps(merged_state, ensure_ascii=False)
        account.last_keepalive_at = now_utc
        account.last_cookie_refresh_at = now_utc
        account.keepalive_note = "抖音浏览器被动同步最新登录态"
        if account.identity_mode == "native":
            account.ua = ua
        session.add(account)
        if recipe:
            value = {k: v for k, v in recipe.items() if k != "saved"}
            value["binding"] = binding
            key = context_key(account.id)
            row = session.get(AppSetting, key) or AppSetting(key=key)
            row.value = json.dumps(value, ensure_ascii=False)
            session.add(row)
        # Cookie refresh is not a reason to reset cooldown or mark an account
        # active: those transitions remain with the existing health/risk logic.
        session.commit()
    return True


def _persist_douyin_api_cookies(account_id: int, original_state: str, state: dict) -> None:
    """Keep server cookie rotations, but never overwrite a newer manual login."""
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        return
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if account and account.platform == "douyin" and account.storage_state == original_state:
            now_utc = datetime.utcnow()
            account.storage_state = json.dumps(state, ensure_ascii=False)
            account.last_keepalive_at = now_utc
            account.last_cookie_refresh_at = now_utc
            account.keepalive_note = "抖音 API 响应 Cookie 已同步"
            session.add(account)
            session.commit()
