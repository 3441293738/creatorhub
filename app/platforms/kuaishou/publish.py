"""快手创作者服务平台浏览器发布。

当前快手把视频、图文、全景视频合并到了同一个 ``/article/publish/video``
页面；图文不再支持旧的 ``/article/publish/atlas`` 直达地址，需要先进入统一
发布页，再点击「上传图文」标签。上传入口还要求通过真实 file chooser 触发，
直接给旧页面上的第一个 file input 塞图片会命中隐藏的视频 input。
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urlparse

from ...browser.identity import Identity
from ...browser.manager import BrowserManager

PUBLISH_URL = "https://cp.kuaishou.com/article/publish/video"
MANAGE_URL = "https://cp.kuaishou.com/article/manage/video"
VIDEO_URL = PUBLISH_URL
IMAGE_URL = PUBLISH_URL

_IMAGE_TAB = (
    '[role="tab"]:has-text("上传图文")',
    '.ant-tabs-tab:has-text("上传图文")',
    'text=上传图文',
)
_DESC_SEL = (
    '#work-description-edit',
    'div[contenteditable="true"][placeholder*="描述"]',
    'div[contenteditable="true"]',
    'textarea[placeholder*="描述"]',
    'textarea',
)
_SUCCESS_KW = ("发布成功", "作品发布成功", "投稿成功", "提交成功", "审核中")
_VERIFY_KW = ("安全验证", "扫码验证", "短信验证", "验证码", "拖动滑块", "身份验证")
_DEBUG_DIR = Path("./data/debug")


def _log(message: str) -> None:
    print(f"[ks-publish] {message}", flush=True)


def _published_work_url(payload) -> str:
    """从发布成功回包中提取公开作品地址；字段缺失时由调用方回退管理页。"""
    if isinstance(payload, dict):
        for key in ("photoId", "photo_id", "photoIdStr", "photoID",
                    "workId", "work_id"):
            value = str(payload.get(key) or "").strip()
            if (not value.isdigit()
                    and re.fullmatch(r"[0-9A-Za-z_-]{8,}", value)):
                return f"https://www.kuaishou.com/short-video/{value}"
        for value in payload.values():
            found = _published_work_url(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _published_work_url(value)
            if found:
                return found
    return ""


async def _dump(page, tag: str) -> str:
    """保存当前发布页截图和文本，选择器再次改版时可直接定位。"""
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = _DEBUG_DIR / f"ks_publish_{tag}_{stamp}"
        png = str(base.with_suffix(".png"))
        try:
            await page.screenshot(path=png, full_page=True)
        except Exception:
            png = ""
        try:
            text = await page.locator("body").inner_text()
        except Exception:
            text = ""
        base.with_suffix(".txt").write_text(
            f"url: {page.url}\n\n{text[:6000]}", encoding="utf-8")
        return png
    except Exception as exc:
        _log(f"保存诊断快照失败: {exc!r}")
        return ""


async def _click_first_visible(page, selectors, timeout=3000) -> bool:
    for selector in selectors:
        try:
            loc = page.locator(selector)
            for index in range(await loc.count()):
                candidate = loc.nth(index)
                if not await candidate.is_visible():
                    continue
                await candidate.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


async def _fill_first(page, selectors, text, timeout=4000) -> bool:
    for selector in selectors:
        try:
            editor = page.locator(selector).first
            await editor.wait_for(state="visible", timeout=timeout)
            await editor.click(timeout=timeout)
            try:
                await editor.fill(text, timeout=timeout)
            except Exception:
                await page.keyboard.press("Control+A")
                await page.keyboard.type(text, delay=15)
            return True
        except Exception:
            continue
    return False


async def _visible_text(page, keywords) -> str:
    for keyword in keywords:
        try:
            loc = page.get_by_text(keyword, exact=False)
            for index in range(await loc.count()):
                if await loc.nth(index).is_visible():
                    return keyword
        except Exception:
            continue
    return ""


async def _upload_files(page, media_type: str, files: List[str]) -> bool:
    """优先通过上传按钮的 file chooser 选择文件；CSS input 只作兜底。"""
    label = "上传图片" if media_type == "images" else "上传视频"
    want = files if media_type == "images" else files[:1]
    try:
        async with page.expect_file_chooser(timeout=8000) as chooser_info:
            button = page.get_by_role("button", name=label, exact=True)
            clicked = False
            for index in range(await button.count()):
                candidate = button.nth(index)
                if await candidate.is_visible():
                    await candidate.click(timeout=4000)
                    clicked = True
                    break
            if not clicked:
                raise RuntimeError(f"未找到“{label}”按钮")
        chooser = await chooser_info.value
        await chooser.set_files(want)
        return True
    except Exception as chooser_error:
        _log(f"file chooser 上传未命中，尝试 input 兜底: {chooser_error!r}")

    selector = ('input[type="file"][accept*="image"]'
                if media_type == "images"
                else 'input[type="file"][accept*="video"]')
    try:
        target = page.locator(selector).first
        await target.set_input_files(want, timeout=15000)
        return True
    except Exception:
        return False


async def _wait_editor(page, timeout_ms: int) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        for selector in _DESC_SEL:
            try:
                loc = page.locator(selector).first
                if await loc.count() and await loc.is_visible():
                    return True
            except Exception:
                continue
        await page.wait_for_timeout(500)
    return False


async def _primary_publish_button(page):
    """新版提交控件是 div.button-primary，不是 button。只取精确“发布”。"""
    selectors = (
        'div[class*="button-primary"]',
        '[role="button"]',
        'button',
        'div[class*="publish"]',
    )
    for selector in selectors:
        try:
            loc = page.locator(selector)
            for index in range(await loc.count() - 1, -1, -1):
                candidate = loc.nth(index)
                if not await candidate.is_visible():
                    continue
                if ((await candidate.inner_text()) or "").strip() != "发布":
                    continue
                aria_disabled = await candidate.get_attribute("aria-disabled")
                classes = str(await candidate.get_attribute("class") or "").lower()
                if aria_disabled == "true" or "disabled" in classes:
                    continue
                try:
                    if not await candidate.is_enabled():
                        continue
                except Exception:
                    pass
                return candidate
        except Exception:
            continue
    return None


async def _choose_visibility(page, visibility: str) -> None:
    label = {
        "public": "所有人可见",
        "friends": "好友可见",
        "private": "仅自己可见",
    }.get(visibility, "所有人可见")
    if visibility == "public":
        return  # 当前默认即所有人可见，少一次点击更稳。
    try:
        choices = page.get_by_text(label, exact=True)
        for index in range(await choices.count() - 1, -1, -1):
            choice = choices.nth(index)
            if await choice.is_visible():
                await choice.click(timeout=2500)
                return
    except Exception:
        _log(f"未能设置查看权限“{label}”，继续使用页面当前值")


async def publish_kuaishou(mgr: BrowserManager, identity: Identity,
                           storage_state_json: str, media_type: str, title: str,
                           desc: str, media_paths: List[str], topics: str = "",
                           headed: bool = True, timeout_seconds: int = 180,
                           visibility: str = "public", allow_save: bool = True,
                           ) -> Tuple[bool, str, str]:
    """发布一条快手作品，返回 ``(ok, result_url, error)``。"""
    files = [str(Path(path).resolve()) for path in media_paths
             if path and Path(path).exists()]
    if not files:
        return False, "", "没有可用的本地媒体文件(路径不存在)"
    tags = [tag.strip().lstrip("#") for tag in (topics or "").split(",")
            if tag.strip()]
    body = ((title + "\n" if title else "") + (desc or "")
            + ("\n" + " ".join(f"#{tag}" for tag in tags) if tags else ""))
    # 当前发布页明确显示 0/500。
    body = body.strip()[:500]

    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    ok, result_url, error = False, "", ""
    publish_response = {"seen": False, "ok": False, "message": "", "url": ""}

    async def on_response(response):
        if publish_response["seen"] or response.request.method != "POST":
            return
        path = urlparse(response.url).path.lower()
        if "/rest/cp/works/" not in path or "/publish" not in path:
            return
        if any(word in path for word in ("config", "snapshot", "tips")):
            return
        try:
            payload = await response.json()
        except Exception:
            return
        publish_response["seen"] = True
        publish_response["ok"] = payload.get("result") in (1, "1")
        publish_response["message"] = str(
            payload.get("message") or payload.get("error_msg")
            or payload.get("errorMsg") or "")
        if publish_response["ok"]:
            publish_response["url"] = _published_work_url(payload)

    page.on("response", on_response)
    try:
        _log(f"打开统一发布页 media_type={media_type}, files={len(files)}")
        await page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(3500)
        body_text = ""
        try:
            body_text = await page.locator("body").inner_text()
        except Exception:
            pass
        if ("passport" in page.url or "/login" in page.url
                or "立即登录" in body_text):
            await _dump(page, "loggedout")
            return False, "", "logged_out:快手创作平台未登录"

        if media_type == "images":
            if not await _click_first_visible(page, _IMAGE_TAB, timeout=3500):
                await _dump(page, "no-image-tab")
                return False, "", "未找到“上传图文”标签(发布页可能改版)"
            await page.wait_for_timeout(700)

        if not await _upload_files(page, media_type, files):
            await _dump(page, "upload-failed")
            return False, "", "上传文件失败:未找到可用的快手上传入口"

        editor_timeout = 120000 if media_type == "video" else 45000
        if not await _wait_editor(page, editor_timeout):
            await _dump(page, "no-editor")
            return False, "", ("文件已选择，但上传后未进入编辑页。请查看弹出的快手窗口，"
                                  "确认文件格式、尺寸或转码状态")

        if body and not await _fill_first(page, _DESC_SEL, body):
            await _dump(page, "no-editor-fill")
            return False, "", "已上传媒体，但未找到作品描述输入框"
        await _choose_visibility(page, visibility)
        # 当前快手图文页未提供稳定的“允许下载”控件；保留参数兼容队列模型。
        _ = allow_save
        await page.wait_for_timeout(700)

        button = None
        for _index in range(90):
            button = await _primary_publish_button(page)
            if button is not None:
                break
            await page.wait_for_timeout(1000)
        if button is None:
            await _dump(page, "no-publish-button")
            return False, "", "未找到可用的“发布”按钮(已保存 data/debug 诊断快照)"
        await button.scroll_into_view_if_needed(timeout=3000)
        await button.click(timeout=5000)
        _log("已点击发布，等待平台确认")

        waited = 0
        while waited < max(int(timeout_seconds), 180):
            url_lower = page.url.lower()
            if publish_response["seen"]:
                if publish_response["ok"]:
                    ok = True
                    break
                error = ("快手拒绝发布:"
                         + (publish_response["message"] or "未知原因"))
                break
            if any(part in url_lower for part in (
                    "/article/manage", "/content/manage", "/article/list")):
                ok = True
                break
            success = await _visible_text(page, _SUCCESS_KW)
            if success:
                ok = True
                break
            verification = await _visible_text(page, _VERIFY_KW)
            if verification:
                _log(f"等待用户在可见窗口完成“{verification}”")
            await page.wait_for_timeout(1000)
            waited += 1

        if ok:
            # 发布回包有 photoId 时直达作品；否则打开作品管理页。不能保存当前
            # /article/publish/video，否则“查看”只会重新回到上传页。
            current_url = str(page.url or "")
            current_path = urlparse(current_url).path.lower()
            if "/short-video/" in current_path or any(part in current_path for part in (
                    "/article/manage", "/content/manage", "/article/list")):
                result_url = current_url
            else:
                result_url = publish_response["url"] or MANAGE_URL
        if not ok and not error:
            png = await _dump(page, "unconfirmed")
            error = ("已点击发布但未收到成功回包；请查看快手窗口是否要求验证码/声明/封面。"
                     f"诊断截图: {png or 'data/debug'}")
    except Exception as exc:
        try:
            await _dump(page, "exception")
        except Exception:
            pass
        error = f"发布异常: {exc!r}"
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return ok, result_url, error
