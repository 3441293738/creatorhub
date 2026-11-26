"""视频号发布入口(浏览器自动化视频号助手 channels.weixin.qq.com)。

视频号发布页是 wujie 微前端(内容在 shadowRoot 里)。Patchright 的定位器默认会穿透
**开放的** shadow DOM,故下面用普通 CSS 选择器即可;若视频号把 shadow root 设成 closed
则需改用 CDP pierce(参见 _CHANNELS_* 注释)。

"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, List, Tuple
from urllib.parse import urlparse

from ...browser.human_interaction import (
    DEFAULT_HUMAN_ENGINE,
    human_click_first,
    human_fill_first,
    human_wait,
)
from ...browser.identity import Identity
from ...browser.manager import BrowserManager
from .._submit import notify_stage as _notify_stage, notify_submit as _notify_submit

log = logging.getLogger("creatorhub.channels")

# 视频号发布入口(实测):统一进入 create 页，视频/图文通过点击左侧「视频」「图文」导航切换。
CREATE_URL_VIDEO = "https://channels.weixin.qq.com/platform/post/create"
CREATE_URL = CREATE_URL_VIDEO   # 兼容旧引用

# 标题/描述编辑器(wujie shadowRoot 内)。视频号「短标题」与「描述」是两个框,
# 这里优先填描述编辑器;短标题选择器同时兼容新版 .form-item 结构。
_DESC_SEL = [
    '.post-view .post-desc-box .input-editor',
    '.post-desc-box .input-editor',
    'div.input-editor[contenteditable="true"]',
    'textarea[placeholder*="描述"]',
    'div[contenteditable="true"]',
]
_SHORT_TITLE_SEL = [
    '.form .form-item input[placeholder*="标题"]',
    '.post-view .short-title-wrap input',
    'input[placeholder*="标题"]',
]
# 文件上传 input(视频号发布页可能在 wujie iframe / shadowRoot 里)
_FILE_SEL = [
    '.post-view .upload input[type="file"]',
    '.upload input[type="file"]',
    '.post-view input[type="file"]',
    'input[type="file"][accept*="video"]',
    'input[type="file"]',
]
# 上传区(点击会弹原生文件对话框,必须用 expect_file_chooser 拦截,不能裸点)。
# 真实 UI:虚线「+」框,内含「上传时长8小时内…」提示文案。
_UPLOAD_ZONE = [
    'text=上传时长', 'text=上传图片', 'text=从这里上传',
    '.center-upload', '.finder-upload', '[class*="upload-entry"]',
    '[class*="upload"]',
]
# 左侧内容管理导航:视频 / 图文(点它进对应「列表页」)
_IMAGE_NAV = [
    'text=图文', 'a:has-text("图文")', 'li:has-text("图文")',
    '[class*="menu"]:has-text("图文")',
]
# 图文列表页里的「发表图文」按钮(在 micro/content iframe 内,必须跨 frame 点)
_CREATE_IMAGE_BTN = ['text=发表图文', 'button:has-text("发表图文")',
                     '[class*="btn"]:has-text("发表图文")']
# 发布按钮(兼容 .post-view 与新版 .form-btns 容器)
_PUBLISH_BTN = [
    '.post-view .form-btns button.weui-desktop-btn_primary',
    '.post-view .form-btns button',
    '.form-btns button.weui-desktop-btn_primary',
    '.form-btns button',
    'button:has-text("发表")',
    'button:has-text("发布")',
]
_DRAFT_BTN = [
    '.post-view .form-btns button:has-text("保存草稿")',
    '.form-btns button:has-text("保存草稿")',
    'button:has-text("保存草稿")',
    'button:has-text("存草稿")',
    'button:has-text("暂存草稿")',
]
_COVER_TRIGGER_SEL = [
    '.post-view [class*="cover"] :text-is("更换封面")',
    '.post-view [class*="cover"] :text-is("编辑封面")',
    '.post-view [class*="cover"] :text-is("设置封面")',
    'text=更换封面',
    'text=编辑封面',
    'text=设置封面',
    '.cover-edit',
    '.edit-cover',
]
_COVER_UPLOAD_SEL = [
    '.weui-desktop-dialog :text-is("上传封面")',
    '.weui-desktop-dialog :text-is("本地上传")',
    '.weui-desktop-dialog :text-is("从本地上传")',
    '[class*="cover-dialog"] :text-is("上传封面")',
    'text=上传封面',
    'text=从本地上传',
]
_COVER_INPUT_SEL = [
    '.weui-desktop-dialog input[type="file"][accept*="image"]',
    '[class*="cover"] input[type="file"][accept*="image"]',
    '.weui-desktop-dialog input[type="file"]',
    'input[type="file"][accept*="image"]',
]
_COVER_CONFIRM_SEL = [
    '.weui-desktop-dialog button:has-text("确认")',
    '.weui-desktop-dialog button:has-text("确定")',
    '.weui-desktop-dialog button:has-text("完成")',
    'button.weui-desktop-btn_primary:has-text("确认")',
    'button.weui-desktop-btn_primary:has-text("确定")',
    'button:has-text("确认")',
]
# 原创声明提示拦截窗、校验/扫码弹窗及预览区进度/报错控件
_NO_TIP_SEL = ['.original-intercept-wrapper .intercept-original-dialog .no-tip-btn']
_PROGRESS_TEXT_SEL = ['.post-view .preview .ant-progress-text', '.preview .ant-progress-text']
_IMG_LOADING_MASK_SEL = ['.form .post-img-list-wrap .thumb-item .loading-mask']
_MEDIA_STATUS_ERR_SEL = ['.post-view .preview .media-status-content .error', '.preview .media-status-content .error']
_CHECK_DIALOG_SEL = ['.post-view .post-check-dialog .check-content', '.post-check-dialog .check-content']
_QR_GUIDE_SEL = ['.post-view .mobile-guide-qr-code .weui-desktop-dialog__title', '.mobile-guide-qr-code .weui-desktop-dialog__title']
# 发布成功判据(页面跳转/出现提示)
_OK_TEXTS = ["发表成功", "发布成功", "提交成功"]
_DRAFT_OK_TEXTS = ["保存成功", "草稿保存成功", "已保存到草稿箱", "保存草稿成功", "已存入草稿箱"]
_UPLOAD_BUSY_KW = ("正在上传", "上传中", "转码中", "处理中")
_UPLOAD_ERR_KW = ("上传失败", "转码失败", "格式不支持", "超出时长", "超过大小")
_VERIFY_KW = ("扫码验证", "人脸验证", "安全验证", "滑块验证", "身份验证", "请用官方发布页面完成验证")
MANAGE_URL = "https://channels.weixin.qq.com/platform/post/list"
DRAFT_URL = "https://channels.weixin.qq.com/platform/post/draft"

# ── 位置 POI(可选;选择器需真号校准,任何一步失败都跳过、不阻塞发布)──
_POI_TRIGGER = [
    'text=不显示位置', '[class*="position"]', '[class*="location"]',
    '.location-display', '.position-select',
]
_POI_INPUT = [
    'input[placeholder*="搜索位置"]', 'input[placeholder*="搜索地点"]',
    'input[placeholder*="位置"]', '[class*="position"] input',
    '[class*="location"] input',
]
_POI_RESULT = [
    '[class*="position"] [class*="item"]', '[class*="location"] [class*="item"]',
    '[class*="poi"] [class*="item"]', '[class*="position"] li',
    '.dropdown-item', '[class*="option"]',
]


async def _fill_first(page, selectors, text, timeout=2500) -> bool:
    return bool(await human_fill_first(page, selectors, text, clear=True, timeout=timeout))


async def _click_first(page, selectors, timeout=3000) -> bool:
    return await human_click_first(page, selectors, timeout=timeout)


async def _click_in_frames(page, selectors, timeout=4000) -> bool:
    """在主页面 + 所有子 frame(micro/content iframe)里点第一个命中的可见元素。
    视频号发布 UI 在 iframe 里,普通 page.click 点不到,必须遍历 frames。"""
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel)
                count = min(await loc.count(), 8)
                for idx in range(count):
                    cand = loc.nth(idx)
                    try:
                        if await cand.is_visible():
                            await DEFAULT_HUMAN_ENGINE.click_locator(
                                cand, page=page, timeout=timeout
                            )
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
    return False


async def _set_location(page, location: str):
    """设置视频号位置 POI(可选,best-effort)。任何一步失败都只记日志、跳过,不影响发布。
    流程:点开位置控件 -> 输入搜索 -> 点第一个结果。选择器需真号校准(见 _POI_*)。"""
    if not location:
        return
    try:
        if not await _click_in_frames(page, _POI_TRIGGER, timeout=3000):
            log.warning("[channels_publish] 未找到位置控件,跳过位置设置")
            return
        await human_wait(page, 1200)
        inp, _fr = await _find_in_frames(page, _POI_INPUT)
        if inp is None:
            log.warning("[channels_publish] 未找到位置搜索框,跳过位置(位置选择器需校准 _POI_INPUT)")
            return
        await DEFAULT_HUMAN_ENGINE.type_text(inp, location, page=page, clear=True, timeout=3000)
        await human_wait(page, 2200)      # 等搜索结果回来
        if not await _click_in_frames(page, _POI_RESULT, timeout=3000):
            log.warning("[channels_publish] 位置「%s」无匹配结果或结果选择器需校准 _POI_RESULT", location)
        else:
            log.info("[channels_publish] 已设置位置: %s", location)
    except Exception as e:
        log.warning("[channels_publish] 设置位置异常(已跳过): %r", e)


async def _find_in_frames(page, selectors):
    """在主页面 + 所有子 frame(wujie iframe)里找第一个命中的定位器。
    Patchright 定位器默认穿透**开放** shadowRoot,但不穿 iframe,故需遍历 page.frames。
    返回 (locator, frame) 或 (None, None)。"""
    for frame in page.frames:                # page.frames[0] 即主 frame
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if await loc.count():
                    return loc, frame
            except Exception:
                continue
    return None, None


async def _collect_diag(page, tag: str) -> str:
    """采集发布页真实 DOM 诊断,返回**紧凑单行摘要**(会拼进 UI 错误文案,你不用翻控制台),
    同时打到服务端日志。每个 frame 报:普通/shadow 里的 file input 数、upload 元素数、
    .post-view 数、按钮文案。"""
    parts = []
    try:
        for i, fr in enumerate(page.frames):
            try:
                info = await fr.evaluate("""() => {
                    const q = (s) => document.querySelectorAll(s).length;
                    let sf = 0;
                    const walk = (root) => root.querySelectorAll('*').forEach(el => {
                        if (el.shadowRoot) { sf += el.shadowRoot.querySelectorAll('input[type=file]').length; walk(el.shadowRoot); }
                    });
                    try { walk(document); } catch(e){}
                    const btns = [...document.querySelectorAll('button,[role=button],.weui-desktop-btn')]
                        .map(b => (b.innerText||'').trim()).filter(Boolean).slice(0, 8);
                    const host = (location.host||'') + (location.pathname||'');
                    return `${host} fi=${q('input[type=file]')} sf=${sf} up=${q('[class*=upload]')} pv=${q('.post-view')} btn=[${btns.join('/')}]`;
                }""")
                parts.append(f"f{i}:{info}")
            except Exception:
                parts.append(f"f{i}:eval_err")
    except Exception:
        pass
    summary = " | ".join(parts) or "无 frame 信息"
    log.warning("[channels_publish/%s] %s", tag, summary)
    return summary


async def _any_text_in_frames(page, keywords) -> str:
    for frame in page.frames:
        for keyword in keywords:
            try:
                loc = frame.get_by_text(keyword, exact=False)
                for index in range(await loc.count()):
                    if await loc.nth(index).is_visible():
                        return keyword
            except Exception:
                continue
    return ""


async def _find_enabled_publish_button(page, *, save_as_draft: bool = False):
    """Find a visible and enabled publish or save-draft button across main/child frames."""
    selectors = _DRAFT_BTN if save_as_draft else _PUBLISH_BTN
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel)
                for index in range(await loc.count() - 1, -1, -1):
                    cand = loc.nth(index)
                    if not await cand.is_visible():
                        continue
                    txt = ((await cand.inner_text()) or "").strip()
                    if save_as_draft:
                        if "草稿" not in txt or "发表" in txt or "发布" in txt:
                            continue
                    else:
                        if "草稿" in txt:
                            continue
                    disabled_attr = await cand.get_attribute("disabled")
                    aria_disabled = await cand.get_attribute("aria-disabled")
                    classes = str(await cand.get_attribute("class") or "").lower()
                    if (disabled_attr is not None or aria_disabled == "true"
                            or "disabled" in classes):
                        continue
                    try:
                        if not await cand.is_enabled():
                            continue
                    except Exception:
                        pass
                    return cand, frame
            except Exception:
                continue
    return None, None


async def _apply_channels_cover(page, cover_path: str, on_stage=None) -> str:
    """跨 frame 为视频号视频上传自定义封面并确认，成功返回空串。"""
    resolved = str(Path(cover_path).resolve())
    if not Path(resolved).is_file():
        return f"自定义封面文件不存在: {cover_path}"
    await _notify_stage(on_stage, "正在上传视频号自定义视频封面…")
    fc_state = {"handled": 0}

    def _on_fc(fc):
        fc_state["handled"] += 1
        asyncio.ensure_future(fc.set_files(resolved))

    page.on("filechooser", _on_fc)
    try:
        if not await _click_in_frames(page, _COVER_TRIGGER_SEL, timeout=3500):
            return "未找到视频号「更换封面 / 编辑封面」入口，已停止提交以防使用默认封面"
        await human_wait(page, 1200)

        uploaded = fc_state["handled"] > 0
        if not uploaded:
            await _click_in_frames(page, _COVER_UPLOAD_SEL, timeout=2500)
            await human_wait(page, 900)
            uploaded = fc_state["handled"] > 0

        if not uploaded:
            for frame in page.frames:
                for sel in _COVER_INPUT_SEL:
                    try:
                        loc = frame.locator(sel)
                        for idx in range(await loc.count()):
                            cand = loc.nth(idx)
                            acc = str(await cand.get_attribute("accept") or "").lower()
                            if "video" in acc or "mp4" in acc:
                                continue
                            await cand.set_input_files(resolved, timeout=10000)
                            uploaded = True
                            break
                        if uploaded:
                            break
                    except Exception:
                        continue
                if uploaded:
                    break

        if not uploaded:
            return "已打开视频号封面弹窗，但未找到封面图片上传控件"

        await human_wait(page, 1500)
        confirmed = False
        for _ in range(5):
            if await _click_in_frames(page, _COVER_CONFIRM_SEL, timeout=2500):
                confirmed = True
                await human_wait(page, 900)
            elif confirmed:
                break
            else:
                await human_wait(page, 500)
        if not confirmed:
            return "已选择视频号自定义封面图，但未找到弹窗「确认/完成」按钮"
        log.info("[channels_publish] 已完成视频号自定义封面设置")
        return ""
    except Exception as exc:
        return f"设置视频号自定义封面异常: {exc!r}"
    finally:
        try:
            page.remove_listener("filechooser", _on_fc)
        except Exception:
            pass


async def _first_text_in_frames(page, selectors) -> str:
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    txt = (await loc.inner_text() or "").strip()
                    if txt:
                        return txt
            except Exception:
                continue
    return ""


async def _dismiss_original_intercept(page) -> None:
    """自动关闭原创声明提示弹窗，避免遮挡后续操作。"""
    for frame in page.frames:
        for sel in _NO_TIP_SEL:
            try:
                loc = frame.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await DEFAULT_HUMAN_ENGINE.click_locator(loc, page=page, timeout=1500)
                    await human_wait(page, 200)
                    return
            except Exception:
                continue


async def _wait_upload_ready(page, media_type: str, timeout_seconds: int,
                             on_stage=None) -> Tuple[bool, str]:
    """监听预览区上传进度、图文加载遮罩与媒体报错状态。"""
    budget = max(1, min(int(timeout_seconds), 180 if media_type == "video" else 60))
    deadline = asyncio.get_running_loop().time() + budget
    last_prog = ""
    while asyncio.get_running_loop().time() < deadline:
        await _dismiss_original_intercept(page)
        media_err = await _first_text_in_frames(page, _MEDIA_STATUS_ERR_SEL)
        if media_err:
            return False, f"视频号媒体处理失败({media_err})"
        err_kw = await _any_text_in_frames(page, _UPLOAD_ERR_KW)
        if err_kw:
            return False, f"视频号媒体上传失败({err_kw})"
        prog_txt = await _first_text_in_frames(page, _PROGRESS_TEXT_SEL)
        mask_count = 0
        if media_type == "images":
            for frame in page.frames:
                for sel in _IMG_LOADING_MASK_SEL:
                    try:
                        c = await frame.locator(sel).count()
                        if c:
                            mask_count = max(mask_count, int(c))
                    except Exception:
                        continue
        prog_busy = bool(prog_txt and "%" in prog_txt and "100%" not in prog_txt)
        if prog_busy and prog_txt != last_prog:
            last_prog = prog_txt
            await _notify_stage(on_stage, f"正在上传视频（{prog_txt}）…")
        elif mask_count > 0:
            msg = f"正在上传图片（还剩 {mask_count} 张）…"
            if msg != last_prog:
                last_prog = msg
                await _notify_stage(on_stage, msg)
        desc_el, _ = await _find_in_frames(page, _DESC_SEL)
        title_el, _ = await _find_in_frames(page, _SHORT_TITLE_SEL)
        editor = desc_el or title_el
        if editor is not None and not prog_busy and mask_count == 0:
            busy_kw = await _any_text_in_frames(page, _UPLOAD_BUSY_KW)
            if not busy_kw:
                return True, ""
        await human_wait(page, 1000)
    desc_el, _ = await _find_in_frames(page, _DESC_SEL)
    title_el, _ = await _find_in_frames(page, _SHORT_TITLE_SEL)
    if desc_el is not None or title_el is not None:
        return True, ""
    return False, "上传超时或未进入编辑表单"


async def _clear_and_fill(el, page, text: str, *, use_keyboard_fallback: bool = True) -> None:
    _ = use_keyboard_fallback
    await DEFAULT_HUMAN_ENGINE.type_text(el, text, page=page, clear=True, timeout=4000)


def _parse_channels_post_create(payload: object) -> tuple[bool, str, str]:
    """Parse ``post/post_create`` JSON -> ``(accepted, export_id, reject_msg)``."""
    if not isinstance(payload, dict):
        return False, "", ""
    err_code = payload.get("errCode")
    base_resp = payload.get("baseResp") if isinstance(payload.get("baseResp"), dict) else {}
    if err_code is None:
        err_code = base_resp.get("errcode")
    export_id = str(payload.get("exportId") or "").strip()
    if not export_id and isinstance(payload.get("data"), dict):
        export_id = str(payload["data"].get("exportId") or "").strip()
    if err_code in (0, "0") or (err_code is None and export_id):
        return True, export_id, ""
    if err_code is not None:
        msg = str(payload.get("errMsg") or base_resp.get("errmsg") or f"errCode={err_code}")
        return False, "", msg
    return False, "", ""


async def publish_channels(mgr: BrowserManager, identity: Identity,
                           storage_state_json: str, media_type: str, title: str,
                           desc: str, media_paths: List[str], topics: str = "",
                           headed: bool = True, timeout_seconds: int = 180,
                           location: str = "",
                           cover_path: str = "", save_as_draft: bool = False,
                           on_submit: Callable[[], object | Awaitable[object]] | None = None,
                           on_stage: Callable[[str], object | Awaitable[object]] | None = None,
                           ) -> Tuple[bool, str, str]:
    """发布一条视频号作品（支持自定义视频封面与保存草稿）。返回 (ok, result_url, error)。
    location:可选,视频号位置 POI(best-effort,设不上不影响发布)。
    storage_state_json 仅校验用,实际登录态在该账号持久 profile 里。"""
    files = [str(Path(p)) for p in media_paths if p and Path(p).exists()]
    if not files:
        return False, "", "没有可用的本地媒体文件(路径不存在)"
    short_title = (title or "").strip()[:16]
    tags = [t.strip().lstrip("#") for t in (topics or "").split(",") if t.strip()]
    # 视频号正文:描述 + 话题(标题另填短标题框)
    body = ((desc or "")
            + ("\n" + " ".join(f"#{t}" for t in tags) if tags else "")).strip()[:1000]

    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    ok, result_url, error = False, "", ""
    submitted = {"clicked": False}
    channels_state = {
        "accepted": False,
        "export_id": "",
        "rejected": "",
        "verify_hint": "",
        "logged_out": False,
    }

    async def on_response(response):
        try:
            url = str(response.url or "")
            url_l = url.lower()
            path = urlparse(url).path.lower()
            method = str(getattr(response.request, "method", "") or "").upper()
        except Exception:
            return
        if "/cgi-bin/mmfinderassistant-bin/auth/auth_login_code" in path:
            channels_state["logged_out"] = True
            return
        if "weixin110.qq.com/security/acct/commverifycodecgi" in url_l:
            channels_state["verify_hint"] = "触发视频号滑块/安全验证"
            return
        if "check_finder_comm_face" in path:
            try:
                data = await response.json()
                inner = data.get("data") if isinstance(data, dict) else None
                if isinstance(inner, dict) and inner.get("blockLevel"):
                    channels_state["verify_hint"] = "视频号要求管理员扫码/过脸验证"
            except Exception:
                pass
            return
        if "finder_author_get_post_verify_info" in path:
            try:
                data = await response.json()
                if isinstance(data, dict) and (data.get("qrCodeToken") or (isinstance(data.get("data"), dict) and data["data"].get("qrCodeToken"))):
                    channels_state["verify_hint"] = "视频号发布需要扫码/过脸验证"
            except Exception:
                pass
            return
        if not submitted["clicked"] or method != "POST":
            return
        if save_as_draft:
            if not any(seg in path for seg in ("/post/post_draft", "/post/save_draft", "/post/draft")):
                return
        elif "/cgi-bin/mmfinderassistant-bin/post/post_create" not in path:
            return
        try:
            payload = await response.json()
        except Exception:
            if int(getattr(response, "status", 200) or 200) >= 400:
                channels_state["rejected"] = f"HTTP {response.status}"
            return
        accepted, export_id, reject_msg = _parse_channels_post_create(payload)
        if accepted:
            channels_state["accepted"] = True
            if export_id:
                channels_state["export_id"] = export_id
        elif reject_msg:
            channels_state["rejected"] = reject_msg

    try:
        page.on("response", on_response)
    except Exception:
        pass

    try:
        await _notify_stage(on_stage, "正在打开视频号助手发布页…")
        # 视频号发布入口就是 create 页;图文/视频靠点左侧导航切换(finderNewLifeCreateg 会 302 回 create)
        await page.goto(CREATE_URL_VIDEO, wait_until="domcontentloaded", timeout=40000)
        await human_wait(page, 4000)
        if ("login.html" in page.url or page.url.rstrip("/").endswith("/login")
                or channels_state["logged_out"]):
            return False, "", f"logged_out:视频号助手未登录(落到 {page.url})"

        # 图文:点左侧「图文」→ 图文列表页 → 点「发表图文」→ 图文发布表单
        # (视频号 UI 在 micro/content iframe 里,按钮要跨 frame 点)
        if media_type == "images":
            await _click_in_frames(page, _IMAGE_NAV, timeout=4000)
            await human_wait(page, 2000)
            if await _click_in_frames(page, _CREATE_IMAGE_BTN, timeout=5000):
                await human_wait(page, 2500)
            else:
                diag = await _collect_diag(page, "no-create-image-btn")
                return False, "", f"未找到「发表图文」按钮,无法进图文发布页。DOM诊断: {diag}"

        await _notify_stage(on_stage, "正在上传媒体并等待处理…")
        want = files if media_type == "images" else files[:1]
        uploaded = False
        # 首选:直接给隐藏 <input type=file> 塞文件(不弹原生对话框,最稳)
        up, _fr = await _find_in_frames(page, _FILE_SEL)
        if up is not None:
            try:
                await up.set_input_files(want, timeout=20000)
                uploaded = True
            except Exception:
                uploaded = False
        # 兜底:input 是点击时按需创建 —— 用 expect_file_chooser 拦截原生文件框(关键!
        # 裸点上传区会弹 Windows「打开」对话框把 Patchright 卡死,必须这样接管)
        if not uploaded:
            try:
                async with page.expect_file_chooser(timeout=10000) as fc_info:
                    if not await _click_in_frames(page, _UPLOAD_ZONE, timeout=4000):
                        raise RuntimeError("未点到上传区")
                chooser = await fc_info.value
                await chooser.set_files(want)
                uploaded = True
            except Exception:
                uploaded = False
        if not uploaded:
            diag = await _collect_diag(page, "upload-failed")
            return False, "", (f"上传失败(未找到可用上传入口/文件选择器)。DOM诊断: {diag}")

        # 主动等待上传/转码就绪并监测上传失败提示
        ready, upload_err = await _wait_upload_ready(
            page, media_type, timeout_seconds, on_stage=on_stage)
        if not ready:
            diag = await _collect_diag(page, "upload-not-ready")
            return False, "", f"{upload_err}。DOM诊断: {diag}"

        await _notify_stage(on_stage, "正在填写视频号标题、描述与位置…")
        if short_title:
            el, _tf = await _find_in_frames(page, _SHORT_TITLE_SEL)
            if el is not None:
                try:
                    await _clear_and_fill(el, page, short_title, use_keyboard_fallback=True)
                except Exception as exc:
                    diag = await _collect_diag(page, "title-fill-failed")
                    return False, "", f"填入视频号短标题失败: {exc!r}。DOM诊断: {diag}"
            elif short_title and short_title not in body:
                body = f"{short_title}\n{body}".strip()[:1000]
        if body:
            el, _df = await _find_in_frames(page, _DESC_SEL)
            if el is None:
                diag = await _collect_diag(page, "no-desc-input")
                return False, "", f"已上传媒体，但未找到视频号描述输入框。DOM诊断: {diag}"
            try:
                await _clear_and_fill(el, page, body, use_keyboard_fallback=True)
            except Exception as exc:
                diag = await _collect_diag(page, "desc-fill-failed")
                return False, "", f"填入视频号描述失败: {exc!r}。DOM诊断: {diag}"

        # 位置 POI(可选,best-effort)
        await _set_location(page, location)
        if cover_path and media_type == "video":
            cover_err = await _apply_channels_cover(page, cover_path, on_stage=on_stage)
            if cover_err:
                diag = await _collect_diag(page, "cover-failed")
                return False, "", f"{cover_err}。DOM诊断: {diag}"
        await _dismiss_original_intercept(page)
        await human_wait(page, 800)

        action_label = "保存草稿" if save_as_draft else "发表"
        await _notify_stage(on_stage, f"正在准备点击「{action_label}」…")
        pub = None
        btn_deadline = asyncio.get_running_loop().time() + max(5, min(120, int(timeout_seconds)))
        while asyncio.get_running_loop().time() < btn_deadline:
            err_kw = (await _first_text_in_frames(page, _MEDIA_STATUS_ERR_SEL)
                      or await _any_text_in_frames(page, _UPLOAD_ERR_KW))
            if err_kw:
                diag = await _collect_diag(page, "upload-err")
                return False, "", f"视频号媒体校验/上传失败({err_kw})。DOM诊断: {diag}"
            pub, _pf = await _find_enabled_publish_button(page, save_as_draft=save_as_draft)
            if pub is not None:
                break
            await human_wait(page, 1000)
        if pub is None:
            diag = await _collect_diag(page, "no-draft-btn" if save_as_draft else "no-publish-btn")
            return False, "", (f"上传/填写已完成但未找到可用的「{action_label}」按钮(可能仍在转码或缺少必填项)。DOM诊断: {diag}")

        await _notify_submit(on_submit)
        submitted["clicked"] = True
        try:
            await DEFAULT_HUMAN_ENGINE.click_locator(pub, page=page, timeout=5000)
            await _notify_stage(on_stage, f"已点击「{action_label}」，等待视频号确认…")
        except Exception as e:
            return False, "", f"write_uncertain:点{action_label}后连接或点击状态异常({e!r})，请先到视频号助手核对，系统不会自动重发"

        # 等成功:监听接口回执、跳到管理/草稿页或出现 toast；同时识别扫码/过脸/滑块验证
        poll_deadline = asyncio.get_running_loop().time() + max(1, int(timeout_seconds))
        verify_notified = False
        ok_texts = _DRAFT_OK_TEXTS if save_as_draft else _OK_TEXTS
        while asyncio.get_running_loop().time() < poll_deadline:
            if channels_state["accepted"]:
                ok = True
                break
            url_l = str(page.url or "").lower()
            if "postlist" in url_l or "/post/list" in url_l or (save_as_draft and "/post/draft" in url_l):
                ok = True
                break
            if await _any_text_in_frames(page, ok_texts):
                ok = True
                break
            qr_title = await _first_text_in_frames(page, _QR_GUIDE_SEL)
            check_msg = await _first_text_in_frames(page, _CHECK_DIALOG_SEL)
            v_kw = await _any_text_in_frames(page, _VERIFY_KW)
            verify_detail = channels_state["verify_hint"] or qr_title or v_kw
            if verify_detail and not verify_notified:
                verify_notified = True
                await _notify_stage(
                    on_stage,
                    f"需要人工安全验证（{verify_detail}），请在弹出的视频号窗口里完成扫码/滑块/人脸验证",
                )
                log.warning("[channels_publish] %s", verify_detail)
            elif check_msg and not verify_notified:
                verify_notified = True
                await _notify_stage(on_stage, f"需要人工安全验证或确认（{check_msg}），请查看弹出的视频号窗口")
                channels_state["verify_hint"] = channels_state["verify_hint"] or check_msg
                log.warning("[channels_publish] check-dialog: %s", check_msg)
            if channels_state["rejected"] and not verify_notified and not channels_state["verify_hint"]:
                diag = await _collect_diag(page, "rejected")
                error = (f"write_uncertain:视频号返回{action_label}异常({channels_state['rejected']})，"
                         f"请到视频号助手确认状态。DOM诊断: {diag}")
                break
            await human_wait(page, 2000)

        if ok:
            current = str(page.url or "")
            if save_as_draft:
                result_url = current if "/post/draft" in current.lower() else DRAFT_URL
            else:
                result_url = current if ("postlist" in current.lower() or "/post/list" in current.lower()) else MANAGE_URL
        elif not error:
            diag = await _collect_diag(page, "no-success")
            hint = channels_state["verify_hint"] or "可能要求封面/实名/扫码或过脸验证"
            error = (f"write_uncertain:已点{action_label}但未确认成功({hint})，请先到视频号助手核对(系统不会自动重发)。"
                     f"当前页: {page.url}; DOM诊断: {diag}")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        prefix = "write_uncertain:" if submitted["clicked"] else ""
        error = f"{prefix}发布异常: {e!r}"
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        try:
            await ctx.close()
        except Exception:
            pass
    return ok, result_url, error
