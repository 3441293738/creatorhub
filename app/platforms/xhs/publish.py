"""小红书发布入口。
优先走「API 直发」(creator_api,参考 Spider_XHS:execjs 签名 + 直连接口,无需浏览器);
若环境缺 Node/execjs/签名 JS,则回退到「浏览器自动化」(实验性)。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import List, Optional, Tuple

from ...browser.identity import Identity
from ...browser.manager import BrowserManager
from .client import XhsApiError, cookie_str_from_state, has_a1


def _result_url(j: dict) -> str:
    d = j.get("data") or {}
    nid = d.get("id") or d.get("note_id") or ""
    return f"https://www.xiaohongshu.com/explore/{nid}" if nid else ""


def _creator_response_error(response):
    """Keep the creator response while making controlled failures classifiable."""
    if not isinstance(response, dict):
        return response or "creator_profile_failed"
    message = str(response.get("msg") or response.get("message") or "")
    code = response.get("code")
    text = message.lower()
    category = signal = ""
    if code == 401:
        category, signal = "auth", "http_401"
    elif code == 407:
        category, signal = "network", "proxy_auth"
    elif code in (403, 429, 461, 471):
        category, signal = "risk", f"http_{code}"
    elif isinstance(code, int) and code >= 500:
        category, signal = "network", f"http_{code}"
    elif any(marker in text for marker in ("登录", "过期", "expired")):
        category, signal = "auth", "auth_expired"
    elif any(marker in text for marker in (
            "risk", "captcha", "风控", "频控", "验证", "限流")):
        category = "risk"
        signal = "platform_risk"
    elif any(marker in text for marker in (
            "timeout", "network", "connection", "proxy", "dns", "tls")):
        category, signal = "network", "network_failure"
    if not category:
        return response or "creator_profile_failed"
    error = XhsApiError(
        message or f"creator response code={code}", category=category,
        status_code=code if isinstance(code, int) else None, signal=signal)
    error.payload = response
    return error


def _publish_api_sync(cookie_str: str, media_type: str, title: str, desc: str,
                      files: List[str], topics: List[str], proxy: str = ""
                      ) -> Tuple[bool, str, str]:
    """同步执行 API 直发(在线程里跑)。返回 (ok, result_url, error)。"""
    from .creator_api import XhsCreatorApi, XhsPublishError
    api = None
    try:
        api = XhsCreatorApi(cookie_str, proxy=proxy)
        if media_type == "video":
            data = Path(files[0]).read_bytes()
            ok, msg, j = api.post_note(media_type="video", title=title, desc=desc,
                                       video_file=data, topics=topics)
        else:
            imgs = [Path(p).read_bytes() for p in files[:18]]
            ok, msg, j = api.post_note(media_type="image", title=title, desc=desc,
                                       image_files=imgs, topics=topics)
        err = "" if ok else (msg or "发布失败")
        if err and any(k in err for k in ("登录", "过期", "expired")):
            err += " —— 请在「账号」里对该小红书账号点「重新登录」(会顺带授权创作平台)"
        return ok, (_result_url(j) if ok else ""), err
    except XhsPublishError as e:
        return False, "", str(e)
    except Exception as e:
        return False, "", f"API 发布异常: {e!r}"
    finally:
        if api:
            api.close()


async def publish_xhs(mgr: BrowserManager, identity: Identity, storage_state_json: str,
                      media_type: str, title: str, desc: str, media_paths: List[str],
                      topics: str = "", headed: bool = True,
                      timeout_seconds: int = 180) -> Tuple[bool, str, str]:
    """发布一条小红书笔记。返回 (ok, result_url, error)。
    API 直发与浏览器兜底都走该账号专属代理 / 持久 profile(防多账号关联)。"""
    files = [str(Path(p)) for p in media_paths if p and Path(p).exists()]
    if not files:
        return False, "", "没有可用的本地媒体文件(路径不存在)"
    cookie_str = cookie_str_from_state(storage_state_json)
    if not has_a1(cookie_str):
        return False, "", "登录态缺少 a1,请重新扫码登录该小红书账号"
    proxy = identity.proxy if identity else ""
    title = (title or "").strip()[:20]
    desc = (desc or "")[:1000]
    tags = [t.strip().lstrip("#") for t in (topics or "").split(",") if t.strip()]

    # 优先 API 直发
    try:
        from . import creator_sign
        api_ok = creator_sign.available()
    except Exception:
        api_ok = False
    if api_ok:
        ok, url, err = await asyncio.to_thread(
            _publish_api_sync, cookie_str, media_type, title, desc, files, tags, proxy)
        if ok or err:
            return ok, url, err
    # 回退:浏览器自动化
    return await _publish_xhs_browser(mgr, identity, media_type,
                                      title, desc, files, tags, headed, timeout_seconds)


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
        login_expired = any(
            marker in (msg or "") for marker in ("登录", "过期", "expired"))
        if not preserve_error:
            return False if login_expired else None
        error = _creator_response_error(response)
        if login_expired and not (
                isinstance(error, XhsApiError) and error.category == "auth"):
            error = XhsApiError(
                msg or "logged_out", category="auth", signal="auth_expired")
            error.payload = response
        if isinstance(error, XhsApiError) and error.category == "auth":
            return False, error
        return None, error
    except Exception as exc:
        return (None, exc) if preserve_error else None


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


# ─────────── 回退:浏览器自动化(实验性)───────────
PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
_TAB_IMAGE = ["text=上传图文", 'div:has-text("上传图文")', "text=图文"]
_TAB_VIDEO = ["text=上传视频", 'div:has-text("上传视频")', "text=视频"]
_TITLE_SEL = ['input[placeholder*="标题"]', ".d-text input", 'input.c-input_inner']
_DESC_SEL = ['[contenteditable="true"]', ".ql-editor", "#post-textarea", "textarea"]
_PUBLISH_BTN = ['button:has-text("发布")', 'div.submit button', "text=发布笔记"]


async def _click_first(page, selectors, timeout=2500) -> bool:
    for sel in selectors:
        try:
            await page.click(sel, timeout=timeout)
            return True
        except Exception:
            continue
    return False


async def _fill_first(page, selectors, text, timeout=2500) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.click(timeout=timeout)
            await el.fill(text, timeout=timeout)
            return True
        except Exception:
            try:
                await page.keyboard.type(text)
                return True
            except Exception:
                continue
    return False


async def _publish_xhs_browser(mgr: BrowserManager, identity: Identity, media_type: str,
                               title: str, desc: str, files: List[str], tags: List[str],
                               headed: bool, timeout_seconds: int) -> Tuple[bool, str, str]:
    body = (desc + ("\n" + " ".join(f"#{t}" for t in tags) if tags else "")).strip()[:1000]
    # 用账号专属持久 profile(独立 UA/代理/指纹);登录态已在 profile 里
    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    ok, result_url, error = False, "", ""
    try:
        await page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(2500)
        if "login" in page.url or "passport" in page.url:
            return False, "", "logged_out:创作平台未登录"
        await _click_first(page, _TAB_VIDEO if media_type == "video" else _TAB_IMAGE)
        await page.wait_for_timeout(1500)
        try:
            await page.locator('input[type="file"]').first.set_input_files(
                files if media_type == "images" else files[:1], timeout=15000)
        except Exception as e:
            return False, "", f"上传文件失败: {e!r}"
        await page.wait_for_timeout(6000 if media_type == "video" else 3500)
        if title:
            await _fill_first(page, _TITLE_SEL, title)
        await page.wait_for_timeout(500)
        if body:
            await _fill_first(page, _DESC_SEL, body)
        await page.wait_for_timeout(800)
        if not await _click_first(page, _PUBLISH_BTN, timeout=4000):
            return False, "", "未找到发布按钮(发布页可能改版)"
        try:
            await page.wait_for_url("**/publish/success**", timeout=20000)
            ok = True
        except Exception:
            try:
                await page.get_by_text("发布成功", exact=False).first.wait_for(timeout=8000)
                ok = True
            except Exception:
                ok = False
        result_url = page.url if ok else ""
        if not ok:
            error = "已点发布但未确认成功(请到小红书确认)"
    except Exception as e:
        error = f"发布异常: {e!r}"
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return ok, result_url, error
