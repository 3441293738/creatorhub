"""手动打开受控账号浏览器窗口与环境自检页面接口。"""
from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from urllib.parse import urlsplit
import asyncio
import json

from fastapi import APIRouter, HTTPException

from app import runtime as app_runtime
from app.browser.douyin_auth import douyin_login_prompt_visible
from app.db import get_session
from app.models import DouyinAccount
from app.platforms.rednote import has_web_session as rednote_has_web_session
from app.risk import OperationKind
from app.services import account_profiles as account_profiles_service
from app.services import login_sessions as login_sessions_service

router = APIRouter()


_PLATFORM_HOST = {"douyin": "douyin.com", "xhs": "xiaohongshu.com",
                  "rednote": "rednote.com",
                  "kuaishou": "kuaishou.com", "shipinhao": "weixin.qq.com",
                  "tiktok": "tiktok.com", "bilibili": "bilibili.com",
                  "instagram": "instagram.com", "twitter": "x.com"}


_XHS_USER_ME_API = "/api/sns/web/v2/user/me"


def _platform_url_allowed(platform: str, value: str) -> bool:
    expected = _PLATFORM_HOST.get(platform, "").casefold()
    try:
        parsed = urlsplit(str(value or "").strip())
        host = (parsed.hostname or "").casefold()
    except (TypeError, ValueError):
        return False
    if parsed.scheme not in {"http", "https"} or not expected:
        return False
    if platform == "twitter":
        return any(
            host == d or host.endswith("." + d)
            for d in ("x.com", "twitter.com")
        )
    return bool(host == expected or host.endswith("." + expected))


def _xhs_open_auth_response_handler(evidence: dict):
    """Collect authoritative XHS login evidence while the headed page loads."""
    async def on_response(response):
        try:
            parsed = urlsplit(str(response.url or ""))
            host = (parsed.hostname or "").lower()
            if not (host == "xiaohongshu.com"
                    or host.endswith(".xiaohongshu.com")):
                return
            if parsed.path.rstrip("/").lower() != _XHS_USER_ME_API:
                return
            evidence["seen"] = True
            if int(response.status) != 200:
                return
            payload = await response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                return
            if data.get("guest") is True:
                evidence["guest"] = True
                return
            if data.get("user_id") or data.get("red_id"):
                evidence["authenticated"] = True
        except Exception:
            return
    return on_response


async def _opened_page_login_state(page, platform: str,
                                   xhs_evidence: dict | None = None) -> str:
    """Classify the opened page without turning transient UI into auth loss."""
    current_url = str(getattr(page, "url", "") or "").lower()
    if platform == "douyin":
        visible = await douyin_login_prompt_visible(page)
        return "logged_out" if visible is True else "unconfirmed"
    if platform == "tiktok":
        if "/login" in current_url or "login?" in current_url:
            return "logged_out"
        try:
            context = await page.evaluate("""() => {
              const el = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
              if (!el || !el.textContent) return null;
              try { return JSON.parse(el.textContent).__DEFAULT_SCOPE__?.['webapp.app-context'] || null; }
              catch (_) { return null; }
            }""")
        except Exception:
            context = None
        user = context.get("user") if isinstance(context, dict) else None
        return "authenticated" if isinstance(user, dict) and (
            user.get("uniqueId") or user.get("uid")) else "unconfirmed"
    if platform == "instagram":
        if "/accounts/login" in current_url or "/challenge/" in current_url or "/checkpoint/" in current_url:
            return "verification" if ("/challenge/" in current_url or "/checkpoint/" in current_url) else "logged_out"
        try:
            cookies = await page.context.cookies("https://www.instagram.com")
            if any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
                return "authenticated"
        except Exception:
            pass
        return "unconfirmed"
    if platform == "twitter":
        if "/account/access" in current_url or "/suspended" in current_url or "/disabled" in current_url:
            return "verification"
        if "/i/flow/login" in current_url or current_url.rstrip("/").endswith("/login"):
            return "logged_out"
        try:
            cookies = await page.context.cookies(["https://x.com", "https://twitter.com"])
            if any(c.get("name") == "auth_token" and c.get("value") for c in cookies):
                return "authenticated"
        except Exception:
            pass
        return "unconfirmed"
    if platform == "xhs_creator":
        if "passport" in current_url:
            return "logged_out"
        try:
            parsed = urlsplit(current_url)
            path = parsed.path.rstrip("/").lower()
            host = (parsed.hostname or "").lower()
        except (TypeError, ValueError):
            path, host = "", ""
        if path == "/login" or path.startswith("/login/"):
            return "logged_out"
        if host == "creator.xiaohongshu.com" and current_url:
            return "authenticated"
        return "unconfirmed"
    if platform == "xhs":
        evidence = xhs_evidence or {}
        if "/website-login/captcha" in current_url \
                or "error_code=300012" in current_url:
            return "verification"
        if "passport" in current_url:
            return "logged_out"
        try:
            path = urlsplit(current_url).path.rstrip("/")
        except (TypeError, ValueError):
            path = ""
        if path == "/login" or path.startswith("/login/"):
            return "logged_out"
        if evidence.get("authenticated"):
            return "authenticated"
        if evidence.get("guest"):
            return "logged_out"
        # The XHS header can briefly render an exact "登录" button before
        # user/me resolves.  A button alone is not proof that the session died;
        # profile refresh remains the authoritative fallback.
        return "unconfirmed"

    logged_out = (
        "passport" in current_url
        or "/login" in current_url
        or "login.html" in current_url
    )
    if not logged_out:
        try:
            logged_out = await page.get_by_text(
                "登录", exact=True).first.is_visible(timeout=1500)
        except Exception:
            logged_out = False
    return "logged_out" if logged_out else "authenticated"


@router.post("/api/accounts/{account_id}/open-browser")
async def open_account_browser(
        account_id: int, url: str = "", environment_check: bool = False):
    """用该账号登录态弹出一个真实浏览器窗口。默认停在平台首页;传 url 则停在该地址
    (仅允许本平台域名,用于「查看」视频号作品/管理页等需登录态才能打开的页面)。
    留给用户手动操作(查看/收发私信、F12 抓接口、手动维护等)。关闭窗口即落盘 Cookie。
    小红书复用账号专属常驻 Context；手动窗口只占用当前账号 Profile，不长期占用
    共享网络出口或全局可视操作锁，因此不同账号/平台窗口可以并存。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        platform = acc.platform
        identity = app_runtime.browser.identity_for(acc)
        has_read_login = (
            account_profiles_service._xhs_has_read_login_state(acc.storage_state)
            if platform == "xhs"
            else rednote_has_web_session(acc.storage_state or "")
            if platform == "rednote"
            else bool(acc.storage_state)
        )
        has_creator_login = bool(acc.creator_storage_state)
        open_scope = (
            "creator" if platform in {"xhs", "rednote"} and not has_read_login
            and has_creator_login else "read"
        )
        states = [acc.storage_state or "", acc.creator_storage_state or ""]
    home = {"xhs": ("https://creator.xiaohongshu.com/"
                     if open_scope == "creator"
                     # /user/profile/me 不再稳定触发 user/me，可能把有效登录态
                     # 显示成“未确认”。首页会加载权威的当前用户接口。
                     else "https://www.xiaohongshu.com/"),
            "kuaishou": "https://www.kuaishou.com/",
            "rednote": ("https://creator.rednote.com/new/note-manager"
                        if open_scope == "creator"
                        else "https://www.rednote.com/zh"),
            "tiktok": "https://www.tiktok.com/",
            "bilibili": "https://www.bilibili.com/",
            "instagram": "https://www.instagram.com/",
            "twitter": "https://x.com/home",
            "shipinhao": "https://channels.weixin.qq.com/platform"}.get(
                platform, "https://www.douyin.com/")
    # 传了 url 且属于本平台域名 -> 停在该地址(否则回首页,防被当跳转开任意站)
    tgt = (url or "").strip()
    if _platform_url_allowed(platform, tgt):
        home = tgt
        if platform == "xhs":
            try:
                open_scope = (
                    "creator" if (urlsplit(home).hostname or "").lower()
                    == "creator.xiaohongshu.com" else "read")
            except (TypeError, ValueError):
                open_scope = "read"

    # Repeated clicks should raise/navigate the already-owned account window,
    # not tear down and cold-start the same Profile again.
    old = app_runtime.open_browsers.get(account_id)
    if old is not None and old.active:
        old_page = getattr(old, "page", None)
        try:
            if old_page is not None and not old_page.is_closed():
                check_opened = False
                foreground_page = old_page
                if environment_check:
                    check_opener = getattr(
                        app_runtime.browser, "open_environment_check", None)
                    if callable(check_opener):
                        check_page = await check_opener(
                            identity, context=old.context, force=True,
                            bring_to_front=True)
                        check_opened = check_page is not None
                        if check_page is not None:
                            foreground_page = check_page
                elif tgt:
                    await old_page.goto(
                        home, wait_until="domcontentloaded", timeout=30000)
                await foreground_page.bring_to_front()
                reused_login_state = "unconfirmed"
                if platform == "douyin" and not environment_check:
                    reused_login_state = await _opened_page_login_state(old_page, platform)
                    if reused_login_state == "logged_out":
                        with get_session() as session:
                            account = session.get(DouyinAccount, account_id)
                            if account:
                                account.status = "invalid"
                                session.add(account)
                                session.commit()
                return {
                    "ok": True,
                    "reused": True,
                    "logged_out": reused_login_state == "logged_out",
                    "login_state": reused_login_state,
                    "login_scope": open_scope,
                    "environment_check_only": environment_check,
                    "environment_check_opened": check_opened,
                    "environment_check_error": "",
                    "environment_check": None,
                }
        except Exception:
            pass
        app_runtime.open_browsers.pop(account_id, None)
        try:
            await old.close()
        except Exception:
            pass
    # 持久 profile 只在"首次空目录"才注入登录态;为防 profile 里 Cookie 缺失/过期导致
    # 打开后未登录,这里用 DB 里已知的登录态 Cookie 再注入一次(覆盖刷新)。
    from app.browser.manager import _sanitize_cookies
    cookies = []
    for st in states:
        if st:
            try:
                cookies.extend(json.loads(st).get("cookies") or [])
            except Exception:
                pass

    @asynccontextmanager
    async def _open_guard():
        # A manually-opened window must reserve only its own account Profile.
        # Holding operation_guard for the whole window lifetime also holds the
        # shared network-exit semaphore; two direct-connect accounts would then
        # look like the browser supports only one window. Production managers
        # expose lock_for(); the fallback keeps lightweight integrations
        # compatible.
        lock_factory = getattr(app_runtime.browser, "lock_for", None)
        if callable(lock_factory):
            async with lock_factory(f"acc:{account_id}"):
                yield identity
            return
        if app_runtime.engine is None:
            yield identity
            return
        async with app_runtime.engine.operation_guard(
                account_id, OperationKind.LOGIN,
                fallback_key=f"open-browser:{account_id}",
                operation_target=identity) as guarded:
            yield guarded

    @asynccontextmanager
    async def _startup_visible_guard():
        # Window startup/navigation is brief and still serialized for XHS so
        # focus and native-page setup cannot race. Once the page is ready the
        # global gate is released; the account-specific Profile lock remains.
        if platform == "xhs":
            async with app_runtime.browser.visible_action(
                    identity, keep_context=True):
                yield
            return
        yield

    guard = _open_guard()
    await guard.__aenter__()
    startup_guard = _startup_visible_guard()
    startup_entered = False
    logged_out = False
    login_state = "unconfirmed"
    check_opened = False
    check_error = ""
    check_page = None
    ctx = None
    try:
        await startup_guard.__aenter__()
        startup_entered = True
        ctx = await app_runtime.browser.open_headed(identity)
        if cookies:
            try:
                await ctx.add_cookies(_sanitize_cookies(cookies))
            except Exception as e:
                print(f"[open-browser] 注入 Cookie 失败: {e!r}")
        check_opener = getattr(app_runtime.browser, "open_environment_check", None)
        if environment_check:
            if not callable(check_opener):
                raise RuntimeError("当前浏览器管理器未启用环境检测")
            check_page = await check_opener(
                identity, context=ctx, force=True, bring_to_front=True)
            if check_page is None:
                raise RuntimeError("环境检测仅适用于 Fingerprint Chromium")
            page = check_page
            check_opened = True
        else:
            # 新 Profile 或环境签名发生变化时，额外打开 BrowserScan 标签；
            # 检测页失败不影响账号平台页和登录态检查。
            if callable(check_opener):
                try:
                    check_page = await check_opener(identity, context=ctx)
                    check_opened = check_page is not None
                except Exception as check_exc:
                    check_error = f"{type(check_exc).__name__}: {check_exc}"
                    print(f"[environment-check] 自动打开失败: {check_exc!r}")
            page = (await app_runtime.browser.new_page(identity, block_media=False)
                    if platform == "xhs" else await ctx.new_page())
            xhs_evidence: dict = {}
            if platform == "xhs" and open_scope == "read":
                page.on("response", _xhs_open_auth_response_handler(xhs_evidence))
            await page.goto(home, wait_until="domcontentloaded", timeout=60000)
            try:
                # Give the async response callback a short opportunity to parse
                # user/me.  This is stronger than inspecting the first rendered
                # header frame, which is often still the anonymous skeleton.
                await page.wait_for_timeout(2500 if platform == "xhs" else 1000)
            except Exception:
                pass
            login_state = await _opened_page_login_state(
                page, ("xhs_creator" if platform == "xhs"
                       and open_scope == "creator" else platform), xhs_evidence)
            logged_out = login_state == "logged_out"
            if logged_out or (platform == "xhs" and login_state == "authenticated"):
                with get_session() as s:
                    opened_account = s.get(DouyinAccount, account_id)
                    if opened_account:
                        other_scope_available = (
                            platform == "xhs" and logged_out and (
                                (open_scope == "read" and has_creator_login)
                                or (open_scope == "creator" and has_read_login)
                            )
                        )
                        if not other_scope_available:
                            opened_account.status = (
                                "invalid" if logged_out else "active")
                        s.add(opened_account)
                        s.commit()
            if platform == "xhs":
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
    except BaseException as e:
        if ctx is not None:
            with suppress(Exception):
                await app_runtime.browser.close_context(identity.key)
        if startup_entered:
            await startup_guard.__aexit__(type(e), e, e.__traceback__)
        await guard.__aexit__(type(e), e, e.__traceback__)
        if not isinstance(e, Exception):
            raise
        raise HTTPException(500, f"打开浏览器失败: {e!r}")
    try:
        await startup_guard.__aexit__(None, None, None)
    except BaseException as e:
        await guard.__aexit__(type(e), e, e.__traceback__)
        raise
    # All headed account contexts are manager-owned. Releasing the manual
    # window through BrowserManager also removes its cache/signature entries, so
    # the next background read cannot reuse an already-closed context.
    close_callback = lambda: app_runtime.browser.close_context(identity.key)
    lease = login_sessions_service._OpenBrowserLease(
        ctx, guard, close_callback=close_callback, page=page)
    app_runtime.open_browsers[account_id] = lease
    try:                       # 用户手动关窗后,从登记表移除
        ctx.on("close", lambda *_: asyncio.create_task(
            login_sessions_service._release_open_browser(account_id, lease)))
        if platform == "xhs":
            page.on("close", lambda *_: asyncio.create_task(
                login_sessions_service._release_open_browser(account_id, lease)))
    except Exception:
        pass
    check_status = None
    status_reader = getattr(app_runtime.browser, "environment_check_status", None)
    if callable(status_reader):
        try:
            check_status = status_reader(identity)
        except Exception:
            pass
    return {"ok": True, "logged_out": logged_out,
            "login_state": login_state, "login_scope": open_scope,
            "environment_check_only": environment_check,
            "environment_check_opened": check_opened,
            "environment_check_error": check_error,
            "environment_check": check_status}


@router.post("/api/accounts/{account_id}/environment-check")
async def open_account_environment_check(account_id: int):
    """在账号自己的独立指纹 Profile 中手动打开 BrowserScan。"""
    return await open_account_browser(account_id, environment_check=True)
