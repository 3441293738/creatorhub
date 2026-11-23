"""交互式登录会话状态跟踪、浏览器窗口租约与原生会话持久化辅助。"""
from __future__ import annotations

import asyncio
import json

from fastapi import HTTPException

from app import runtime as app_runtime
from app.db import get_session
from app.models import DouyinAccount
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


def _active_open_browser_account_ids() -> list[int]:
    return [
        account_id for account_id, lease in app_runtime.open_browsers.items()
        if bool(getattr(lease, "active", True))
    ]


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
                 *, account_id: int | None = None, open_scope: str = "read"):
        self.context = context
        self.guard = guard
        self.close_callback = close_callback
        self.page = page
        self.account_id = account_id
        self.open_scope = open_scope
        self._released = False
        self._closed = False
        self._release_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return not self._released and not self._closed

    async def _persist_context_state(self) -> None:
        if not self.account_id or self.context is None:
            return
        storage_fn = getattr(self.context, "storage_state", None)
        if not callable(storage_fn):
            return
        try:
            state_obj = await storage_fn()
            if not isinstance(state_obj, dict) or not state_obj.get("cookies"):
                return
            state_json = json.dumps(state_obj, ensure_ascii=False)
            with get_session() as session:
                account = session.get(DouyinAccount, self.account_id)
                if account is None:
                    return
                if self.open_scope == "creator" and account.platform in {"xhs", "rednote"}:
                    account.creator_storage_state = state_json
                else:
                    account.storage_state = state_json
                    if account.platform in {"kuaishou", "shipinhao", "bilibili", "instagram", "twitter"}:
                        account.creator_storage_state = state_json
                session.add(account)
                session.commit()
        except Exception:
            # 窗口若已被用户直接点 X 关闭，storage_state() 可能抛 TargetClosedError；静默跳过即可
            pass

    async def release(self) -> None:
        async with self._release_lock:
            if self._released:
                return
            self._released = True
            await self.guard.__aexit__(None, None, None)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                await self._persist_context_state()
                if self.close_callback is not None:
                    await self.close_callback()
                else:
                    await self.context.close()
            finally:
                await self.release()


async def _release_open_browser(account_id: int, lease: _OpenBrowserLease) -> None:
    try:
        await lease.close()
    finally:
        if app_runtime.open_browsers.get(account_id) is lease:
            app_runtime.open_browsers.pop(account_id, None)


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
        account.storage_state = json.dumps(state, ensure_ascii=False)
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
            account.storage_state = json.dumps(state, ensure_ascii=False)
            session.add(account)
            session.commit()
