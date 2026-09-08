"""小红书发布入口：默认可见页面操作，签名 API 仅作显式兼容模式。"""
from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import ExitStack
from functools import partial
from typing import List, Optional, Tuple

from ...browser.identity import Identity
from ...browser.manager import BrowserManager
from .browser_writes import XhsWriteOutcome, publish_xhs_browser
from .client import XhsApiError, cookie_str_from_state, has_a1
from .deadline import deadline_after, remaining_seconds, timeout_error
from .media import snapshot_publish_files, validate_publish_files
from .responses import validate_payload


def _result_url(j: dict) -> str:
    d = j.get("data") or {}
    nid = d.get("id") or d.get("note_id") or ""
    return f"https://www.xiaohongshu.com/explore/{nid}" if nid else ""


def _creator_response_error(response):
    """Classify legacy tuple-returning adapters with the shared envelope rules."""
    if not isinstance(response, dict):
        return response or "creator_profile_failed"
    message = str(response.get("msg") or response.get("message") or "")
    try:
        validate_payload(response)
    except XhsApiError as error:
        if message:
            error.args = (message,)
        return error
    return response or "creator_profile_failed"


def _api_privacy_type(visibility: str) -> int:
    """Creator's privacy_info.type: 0=public, 1=private.

    Keep an explicit map instead of inheriting an upstream demo's default.
    See ReaJason/xhs core.py create_note(is_private) and Spider_XHS's payload
    builders. Additional scopes need their own verified API contract first.
    """
    values = {"public": 0, "private": 1}
    key = str(visibility or "").strip().lower()
    if key not in values:
        raise ValueError("API 发布可见性须为 public（公开）或 private（仅自己可见）")
    return values[key]


def _publish_api_sync(cookie_str: str, media_type: str, title: str, desc: str,
                      files: List[str], topics: List[str], proxy: str = "", *,
                      visibility: str = "public", on_submit=None,
                      cancel_event: threading.Event | None = None,
                      preserve_error: bool = False, deadline: float | None = None,
                      verify_signing: bool = False
                      ) -> Tuple[bool, str, str | XhsApiError]:
    """同步 API 发布；引擎可保留提交前的结构化异常进行风险分类。"""
    try:
        privacy_type = _api_privacy_type(visibility)
    except ValueError as exc:
        return False, "", str(exc)
    from .creator_api import XhsCreatorApi
    submitted = False
    if deadline is None:
        deadline = deadline_after(180)

    def check_active():
        remaining_seconds(deadline, cancel_event)

    def mark_submitted():
        nonlocal submitted
        check_active()
        if on_submit is not None:
            on_submit()
        submitted = True
        check_active()

    def failure(message):
        if submitted:
            message = f"write_uncertain:发布已提交，结果需到平台核对；{message}"
        elif not (preserve_error and isinstance(message, XhsApiError)):
            message = str(message)
        return False, "", message

    try:
        check_active()
        if verify_signing:
            from . import creator_sign
            try:
                api_ok = creator_sign.available()
            except Exception:
                api_ok = False
            check_active()
            if not api_ok:
                return failure("API 兼容模式所需签名环境不可用")
        with ExitStack() as stack:
            # Freeze the full selection on disk before credentials or uploads.
            # Close HTTP transports before deleting the private snapshots.
            blobs = stack.enter_context(snapshot_publish_files(
                media_type, files, check_active=check_active))
            api = XhsCreatorApi(cookie_str, proxy=proxy, cancel_event=cancel_event,
                                deadline=deadline)
            stack.callback(api.close)
            if media_type == "video":
                ok, msg, j = api.post_note(media_type="video", title=title, desc=desc,
                                           video_file=blobs[0], topics=topics,
                                           privacy_type=privacy_type, on_submit=mark_submitted)
            else:
                ok, msg, j = api.post_note(media_type="image", title=title, desc=desc,
                                           image_files=blobs, topics=topics,
                                           privacy_type=privacy_type, on_submit=mark_submitted)
            check_active()
            err = "" if ok else (msg or "发布失败")
            if err and any(k in err for k in ("登录", "过期", "expired")):
                err += " —— 请在「账号」里完成该小红书账号的「创作者登录」"
            return (True, _result_url(j), "") if ok else failure(err)
    except XhsApiError as e:
        return failure(e)
    except Exception as e:
        return failure(f"API 发布异常: {e!r}")


async def publish_xhs(mgr: BrowserManager, identity: Identity, storage_state_json: str,
                      media_type: str, title: str, desc: str, media_paths: List[str],
                      topics: str = "", headed: bool = True,
                      timeout_seconds: int = 180,
                      mode: str = "browser",
                      visibility: str = "public",
                      on_submit=None, preserve_error: bool = False
                      ) -> Tuple[bool, str, str | XhsApiError]:
    """发布一条小红书笔记。返回 (ok, result_url, error)。
    页面模式使用账号持久 Profile；API 仅为显式兼容模式。"""
    mode = str(mode or "browser").strip().lower()
    if mode not in {"browser", "api"}:
        mode = "browser"
    try:
        deadline = deadline_after(timeout_seconds) if mode == "api" else None
        files = validate_publish_files(media_type, media_paths)
    except ValueError as exc:
        return False, "", str(exc)
    cookie_str = cookie_str_from_state(storage_state_json)
    proxy = identity.proxy if identity else ""
    title = (title or "").strip()[:20]
    desc = (desc or "")[:1000]
    tags = [t.strip().lstrip("#") for t in (topics or "").split(",") if t.strip()]

    if mode == "browser":
        outcome = await publish_xhs_browser(
            mgr, identity, media_type, title, desc, tags, files,
            timeout_seconds=timeout_seconds, visibility=visibility,
            on_submit=on_submit)
        return outcome.legacy()

    # 显式 API 兼容模式；失败后不切换到浏览器，避免一次任务被重复提交。
    try:
        _api_privacy_type(visibility)
    except ValueError as exc:
        return False, "", str(exc)
    if not has_a1(cookie_str):
        return False, "", "登录态缺少 a1,请重新扫码登录该小红书账号"
    # The worker must finish before account locks are released. Cancelling an
    # asyncio.to_thread await alone does not stop its underlying HTTP requests.
    cancel_event = threading.Event()
    loop = asyncio.get_running_loop()
    submit_future = None

    async def notify_submit():
        remaining_seconds(deadline, cancel_event)
        if on_submit is not None:
            result = on_submit()
            if inspect.isawaitable(result):
                await result

    def notify_from_worker():
        nonlocal submit_future
        remaining_seconds(deadline, cancel_event)
        submit_future = asyncio.run_coroutine_threadsafe(notify_submit(), loop)
        try:
            submit_future.result(timeout=remaining_seconds(deadline, cancel_event))
        except FutureTimeoutError:
            if submit_future.done():
                # A callback may raise its own TimeoutError before our budget.
                remaining_seconds(deadline, cancel_event)
                raise
            submit_future.cancel()
            raise timeout_error()
        except Exception:
            submit_future.cancel()
            remaining_seconds(deadline, cancel_event)
            raise

    def expire():
        cancel_event.set()
        if submit_future is not None:
            submit_future.cancel()

    work = partial(
        _publish_api_sync, cookie_str, media_type, title, desc, files, tags, proxy,
        visibility=visibility, on_submit=notify_from_worker, cancel_event=cancel_event,
        preserve_error=preserve_error, deadline=deadline, verify_signing=True)
    # Keep the executor future outside all_tasks(): loop-wide task cancellation
    # must not cancel only the wrapper while its publishing thread keeps going.
    worker = loop.run_in_executor(None, contextvars.copy_context().run, work)
    try:
        delay = remaining_seconds(deadline)
    except XhsApiError:
        delay = 0
    timeout_handle = loop.call_later(delay, expire)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel_event.set()
        if submit_future is not None:
            submit_future.cancel()
        # Even repeated cancellation must not leave a live publishing worker
        # using the account after its operation guard has been released.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise
    finally:
        timeout_handle.cancel()


async def creator_check(storage_state_json: str, proxy: str = "", *,
                        preserve_error: bool = False):
    """校验创作者登录态。True=有效,False=确已失效,None=不确定(网络/环境,勿据此判失效)。"""
    cookie_str = cookie_str_from_state(storage_state_json)
    if not has_a1(cookie_str):
        error = XhsApiError(
            "登录态缺少 a1", category="auth", signal="auth_expired")
        return (False, error) if preserve_error else False
    try:
        from . import creator_sign
        if not creator_sign.available():
            return (None, "creator_sign_unavailable") if preserve_error else None
    except Exception as exc:
        return (None, exc) if preserve_error else None

    def _run():
        from .creator_api import XhsCreatorApi
        api = XhsCreatorApi(cookie_str, proxy=proxy)
        try:
            result = api.ping(detailed=True)
            if len(result) == 3:
                return result
            ok, msg = result
            return ok, msg, {"success": ok, "msg": msg}
        finally:
            api.close()
    try:
        ok, msg, response = await asyncio.to_thread(_run)
        if ok:
            return (True, "") if preserve_error else True
        error = _creator_response_error(response)
        valid = False if isinstance(error, XhsApiError) and error.category == "auth" else None
        return (valid, error) if preserve_error else valid
    except Exception as exc:
        valid = False if isinstance(exc, XhsApiError) and exc.category == "auth" else None
        return (valid, exc) if preserve_error else valid


async def creator_profile(storage_state_json: str, proxy: str = "", *,
                          preserve_error: bool = False):
    """用创作平台接口拿账号资料(昵称/小红书号/头像/粉丝/笔记数)。返回 parsed dict 或 None。"""
    cookie_str = cookie_str_from_state(storage_state_json)
    if not has_a1(cookie_str):
        return (None, "logged_out") if preserve_error else None
    try:
        from . import creator_sign
        if not creator_sign.available():
            return (None, "creator_sign_unavailable") if preserve_error else None
    except Exception as exc:
        return (None, exc) if preserve_error else None

    def _run():
        from .creator_api import XhsCreatorApi, parse_creator_user
        api = XhsCreatorApi(cookie_str, proxy=proxy)
        try:
            ok, d, response = api.my_info(detailed=True)
            if ok and d:
                return parse_creator_user(d), ""
            return None, _creator_response_error(response)
        finally:
            api.close()
    try:
        profile, error = await asyncio.to_thread(_run)
        return (profile, error) if preserve_error else profile
    except Exception as exc:
        return (None, exc) if preserve_error else None


_LIST_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


async def list_published(storage_state_json: str, proxy: str = "") -> Tuple[bool, str, list]:
    """获取该账号「已发布作品列表」。用网页 user_posted 接口(按自己的 user_id 拉,
    与监控同一套,稳定可靠);创作平台 note/user/posted 接口已不稳定,不再用。"""
    cookie_str = cookie_str_from_state(storage_state_json)
    if not has_a1(cookie_str):
        return False, "登录态缺少 a1,请重新登录", []
    from .client import XhsApiClient, XhsApiError
    client = XhsApiClient(cookie_str, _LIST_UA, proxy=proxy)
    # 1) 取自己的 user_id
    uid = ""
    try:
        me = await client.self_info()
        uid = str((me or {}).get("user_id") or "")
    except Exception:
        uid = ""
    if not uid:
        prof = await creator_profile(storage_state_json, proxy=proxy)   # 创作平台资料兜底
        uid = (prof or {}).get("sec_uid") or ""
    if not uid:
        return False, "拿不到账号 user_id(请先「刷新资料」或重新登录)", []
    # 2) 拉自己的笔记
    try:
        d = await client.notes_by_creator(uid)
        notes = d.get("notes") or []
        print(f"[xhs_published_web] uid={uid} got={len(notes)} "
              f"note0_keys={list(notes[0].keys()) if notes else []}")
        return True, "ok", notes
    except XhsApiError as e:
        return False, str(e), []
    except Exception as e:
        return False, f"{e!r}", []
