"""抖音发布入口(浏览器自动化抖音创作者服务平台 creator.douyin.com)。

抖音 PC 端上传要分片传 upload 节点 + a_bogus 签名 + create_aweme,纯 HTTP 直发链路长、
易随改版失效;发布是低频写操作,和快手一样走浏览器自动化性价比最高,也贴合本项目
「登录态浏览器 + 免手写签名」的路线:用账号专属持久 profile(已含创作者登录态)打开
发布页,上传文件、填标题/正文、点发布。

⚠️ 实验性:发布页选择器随抖音改版可能失效,集中在下面的 _* 选择器常量;
   发布时弹真实窗口,遇滑块验证 / 需补封面 / 定位话题必填可在窗口里手动处理。

调试:失败(含「已点发布但未确认成功」)时会把截图 + 页面 URL/文本快照写到
   data/debug/dy_publish_*.{png,txt},并在服务端控制台打印 [dy-publish] 日志。
   抖音创作平台改版频繁,首次真机发布基本都要据此把选择器校准一次。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
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

UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
MANAGE_URL = "https://creator.douyin.com/creator-micro/content/manage"
# 图文发布入口(default-tab=3 直达「发布图文」;失败再退回点 tab)
IMAGE_URL = "https://creator.douyin.com/creator-micro/content/upload?default-tab=3"
_TAB_IMAGE = [
    '#root div[class^="tab-container"] div[class^="tab-item"]:has-text("发布图文")',
    'div:has-text("发布图文")', 'text=发布图文', 'text=图文',
]
_IMAGE_INPUTS = (
    '#root div[class*="container-drag"] input[accept^="image"]',
    'input[type="file"][accept*="image"]',
    'input[type="file"]',
)
_VIDEO_INPUTS = (
    '#root div[class*="container-drag"] input[accept^="video"]',
    'input[type="file"][accept*="video"]',
    'input[type="file"]',
)
# 视频有独立「标题」短标题输入;图文有「作品标题」
_TITLE_SEL = ['input[placeholder*="填写作品标题"]', 'input[placeholder*="作品标题"]',
              'input[placeholder*="标题"]', '.title-input input', 'input.semi-input']
# 正文/简介富文本(抖音创作者用 editor-kit 富文本,contenteditable)
_DESC_SEL = ['.editor-kit-editor-container [contenteditable="true"]',
             'div[data-placeholder*="简介"]', 'div[data-placeholder*="作品"]',
             'div[contenteditable="true"]', '.zone-container',
             'textarea[placeholder*="简介"]', 'textarea']
# 最终提交按钮(优先表单底栏容器按钮,再按 enabled + 文本过滤)
_PUBLISH_CONTAINER_BTN = 'div[class*="form-container"] div[class*="container-"] button'
_PUBLISH_BTN = [_PUBLISH_CONTAINER_BTN, 'button:has-text("发布")', 'button:has-text("发布作品")',
                'div[class*="content-confirm"] button', '.publish-btn button', '.button-publish']

_DEBUG_DIR = Path("./data/debug")

# 仅匹配明确提交完成文案(剔除"已发布/审核中/发布完成"等易与静态提示「等待上传发布完成」冲突的词)
_SUCCESS_KW = ("发布成功", "作品发布成功", "投稿成功")
# 仅阻塞真正未完成上传/转码的状态，不等待封面推荐生成与发布助手检测
_UPLOAD_BUSY_KW = ("正在上传", "转码中", "视频处理中")
_UPLOAD_ERR_KW = ("上传失败", "转码失败", "视频过大", "文件过大", "超过限制", "格式不支持", "不支持该格式")
_BLOCKED_KW = ("请选择封面", "请上传封面", "请添加封面", "请等待视频上传", "视频上传中", "标题不能为空", "发布失败")
# 抖音风控人工验证(短信验证码 / 扫码 / 滑块):需用户在弹出窗口里手动完成,不能自动化
_VERIFY_KW = ("接收短信验证码", "短信验证码", "为确保是本人操作", "输入验证码",
              "安全验证", "完成验证", "拖动滑块", "使用原设备扫码", "身份验证")
_VERIFY_WAY_LABELS = {
    "mobile_sms_verify": "短信验证",
    "qr_code_verify": "扫码验证",
    "face_verify": "人脸验证",
}


_pub_log = logging.getLogger("creatorhub.publish.douyin")


def _log(msg: str) -> None:
    if any(k in msg for k in ("失败", "超时", "异常", "验证", "拦截")):
        _pub_log.warning("%s", msg)
    else:
        _pub_log.info("%s", msg)


async def _visible_any(page, keywords) -> str:
    """返回第一个当前可见的关键词文案(无则空串);用于快速轮询,不阻塞等待。"""
    for kw in keywords:
        try:
            if await page.get_by_text(kw, exact=False).first.is_visible():
                return kw
        except Exception:
            continue
    return ""


async def _dump(page, tag: str) -> str:
    """把当前页面截图 + URL/可见文本快照落盘,返回截图路径(供日志/前端提示)。"""
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = _DEBUG_DIR / f"dy_publish_{tag}_{stamp}"
        png = str(base.with_suffix(".png"))
        try:
            await page.screenshot(path=png, full_page=True)
        except Exception:
            png = ""
        try:
            txt = await page.inner_text("body")
        except Exception:
            txt = ""
        base.with_suffix(".txt").write_text(
            f"url: {page.url}\n\n{txt[:4000]}", encoding="utf-8")
        _log(f"已存诊断快照 tag={tag} url={page.url} png={png}")
        return png
    except Exception as e:
        _log(f"存快照失败: {e!r}")
        return ""


async def _click_first(page, selectors, timeout=2500) -> bool:
    return await human_click_first(page, selectors, timeout=timeout)


async def _fill_first(page, selectors, text, timeout=3000) -> bool:
    sel = await human_fill_first(page, selectors, text, clear=True, timeout=timeout)
    if sel:
        _log(f"填入文本用选择器 {sel!r}")
        return True
    return False


async def _wait_editor(page, timeout_ms: int) -> bool:
    """并发轮询所有编辑器候选(= 表单已挂载),避免单选择器顺序阻塞完整超时。"""
    deadline = asyncio.get_running_loop().time() + max(2.0, timeout_ms / 1000)
    while asyncio.get_running_loop().time() < deadline:
        for sel in _DESC_SEL:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    return True
            except Exception:
                continue
        await human_wait(page, 500)
    return False


async def _read_upload_progress(page) -> str:
    """读取预览区上传进度百分比文本。"""
    for sel in (
        'div[class*="phone-screen"] div[class*="upload-progress-"] span[class*="text-"]',
        'div[class*="phone-screen"] div[class*="upload-progress"]',
    ):
        try:
            prog = page.locator(sel).first
            if await prog.count() and await prog.is_visible():
                txt = (await prog.inner_text() or "").strip()
                if txt:
                    return txt
        except Exception:
            continue
    return ""


async def _wait_upload_complete(page, timeout_ms: int, on_stage=None) -> str:
    """等待预览区真实上传进度与转码完成；忽略封面推荐生成及底部静态提示。"""
    deadline = asyncio.get_running_loop().time() + max(5.0, timeout_ms / 1000)
    last_prog = ""
    while asyncio.get_running_loop().time() < deadline:
        err_kw = await _visible_any(page, _UPLOAD_ERR_KW)
        if err_kw:
            return f"抖音媒体上传失败({err_kw})"
        prog_txt = await _read_upload_progress(page)
        prog_busy = bool(prog_txt and "%" in prog_txt and "100%" not in prog_txt)
        busy_kw = await _visible_any(page, _UPLOAD_BUSY_KW)
        if prog_busy:
            if prog_txt != last_prog:
                last_prog = prog_txt
                await _notify_stage(on_stage, f"正在上传视频（{prog_txt}）…")
        elif busy_kw and last_prog != busy_kw:
            last_prog = busy_kw
            await _notify_stage(on_stage, f"正在{busy_kw}…")
        if not prog_busy and not busy_kw:
            return ""
        await human_wait(page, 1500)
    return "抖音视频上传或转码超时，请检查文件后重试"


async def _dismiss_guides(page) -> None:
    """关闭遮挡表单按钮的轻量新手气泡(如「我知道了」)，不碰验证弹窗。"""
    for label in ("我知道了",):
        for finder in (
            lambda lbl: page.get_by_role("button", name=lbl, exact=True),
            lambda lbl: page.get_by_text(lbl, exact=True),
        ):
            try:
                loc = finder(label)
                for idx in range(await loc.count()):
                    b = loc.nth(idx)
                    if await b.is_visible():
                        await DEFAULT_HUMAN_ENGINE.click_locator(b, page=page, timeout=1500)
                        await human_wait(page, 300)
                        return
            except Exception:
                continue


async def _primary_publish_button(page):
    """挑最终「发布」按钮(优先表单底栏容器按钮):仅取可见且非禁用态。"""
    for sel in (_PUBLISH_CONTAINER_BTN, 'button'):
        try:
            btns = page.locator(sel)
            n = await btns.count()
        except Exception:
            continue
        cand = None
        for i in range(n):
            b = btns.nth(i)
            try:
                if not await b.is_visible():
                    continue
                t = ((await b.inner_text()) or "").strip()
                if t not in ("发布", "发布作品", "发布视频", "发布图文"):
                    continue
                disabled_attr = await b.get_attribute("disabled")
                aria_disabled = await b.get_attribute("aria-disabled")
                classes = str(await b.get_attribute("class") or "").lower()
                if (disabled_attr is not None or aria_disabled == "true"
                        or "disabled" in classes):
                    continue
                if not await b.is_enabled():
                    continue
                cand = b   # 页面里可能多个,取靠后的(底部提交)
            except Exception:
                continue
        if cand is not None:
            return cand
    return None


async def _confirm_published_on_manage(page, title: str, *, attempts: int = 4) -> bool:
    """当只看到 toast 而未收到 create_v2/跳转时，进作品管理页核实是否真实入库。"""
    needle = str(title or "").strip()[:30]
    if not needle:
        return False
    try:
        await page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return False
    for index in range(max(1, attempts)):
        try:
            loc = page.get_by_text(needle, exact=False).first
            if await loc.count() and await loc.is_visible():
                return True
        except Exception:
            pass
        if index + 1 < attempts:
            await human_wait(page, 2000)
    return False


async def _dump_radios(page) -> None:
    """把页面上疑似单选项/可点标签的真实文案打出来,便于按实际文案校准。"""
    try:
        texts = await page.eval_on_selector_all(
            'label, [role="radio"], .semi-radio, .semi-radio-addon, [class*="radio"]',
            "els => els.map(e => (e.innerText||'').trim()).filter(t => t && t.length <= 12)")
        seen, out = set(), []
        for t in texts:
            if t not in seen:
                seen.add(t); out.append(t)
        _log(f"页面可选项文案候选: {out[:40]}")
    except Exception as e:
        _log(f"枚举单选项失败: {e!r}")


async def _choose_radio(page, label: str) -> bool:
    """点一个单选项。**只走安全精确策略**,绝不 force / 不点任意祖先 ——
    否则可能误点「继续添加/封面」等触发系统选图框的按钮,卡死自动化。"""
    # 1) 无障碍 role=radio(Semi 给单选组件挂 role)
    try:
        loc = page.get_by_role("radio", name=label, exact=True)
        if await loc.count():
            first_radio = loc.first
            await first_radio.scroll_into_view_if_needed(timeout=2000)
            try:
                if await first_radio.is_checked():
                    _log(f"已选发布设置「{label}」(role-checked)")
                    return True
            except Exception:
                pass
            await DEFAULT_HUMAN_ENGINE.click_locator(first_radio, page=page, timeout=2500)
            _log(f"已选发布设置「{label}」(role)")
            return True
    except Exception:
        pass
    # 2) 仅在 Semi 单选组件内、且整项文本恰等于 label 的元素上点(不碰其它任何东西)
    try:
        radios = page.locator('label.semi-radio, .semi-radio, label[class*="radio"]')
        n = await radios.count()
        for i in range(n):
            r = radios.nth(i)
            try:
                txt = ((await r.inner_text()) or "").strip()
            except Exception:
                continue
            if txt == label:
                await r.scroll_into_view_if_needed(timeout=2000)
                await DEFAULT_HUMAN_ENGINE.click_locator(r, page=page, timeout=2000)
                _log(f"已选发布设置「{label}」(semi-radio)")
                return True
    except Exception:
        pass
    _log(f"⚠️ 未点中发布设置「{label}」(已安全跳过,不乱点)—— 打印页面实际可选项供校准:")
    await _dump_radios(page)
    return False


def _fix_latin1_utf8(text: str) -> str:
    """Repair HTTP header UTF-8 bytes decoded as Latin-1."""
    raw = str(text or "").strip()
    if not raw or any(ord(ch) > 0xFF for ch in raw):
        return raw
    try:
        return raw.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return raw


def _parse_passport_decision(headers: dict) -> str:
    """Parse Douyin create response's ``x-tt-verify-passport-decision`` header."""
    raw = ""
    for key, value in (headers or {}).items():
        if str(key).lower() == "x-tt-verify-passport-decision":
            raw = str(value or "").strip()
            break
    if not raw:
        return ""
    decision = None
    for candidate in (_fix_latin1_utf8(raw), raw):
        try:
            parsed_top = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed_top, dict):
            decision = parsed_top
            break
    if not isinstance(decision, dict):
        return "需要安全验证"
    hint = ""
    common = decision.get("common_params")
    if isinstance(common, dict) and common.get("copywriting"):
        copy_raw = str(common.get("copywriting") or "")
        for candidate in (
            _fix_latin1_utf8(copy_raw),
            copy_raw,
            copy_raw.encode("latin1", errors="ignore").decode("utf-8", errors="ignore"),
        ):
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                t = _fix_latin1_utf8(parsed.get("title") or "")
                d = _fix_latin1_utf8(parsed.get("desc") or "")
                hint = f"{t}：{d}" if (t and d) else (t or d)
                break
    ways = decision.get("verify_way_name_list")
    if isinstance(ways, str) and ways.strip():
        labels = [_VERIFY_WAY_LABELS.get(w.strip(), w.strip())
                  for w in ways.split(",") if w.strip()]
        if labels:
            way_txt = "、".join(labels)
            hint = f"{hint}（可通过 {way_txt} 完成验证）" if hint else f"可通过 {way_txt} 完成验证"
    return hint or "需要安全验证"


def _parse_create_payload(payload: object) -> tuple[bool, str, str]:
    """Parse ``aweme/create(_v2)`` JSON response -> ``(accepted, post_id, reject_msg)``."""
    if not isinstance(payload, dict):
        return False, "", ""
    status_code = payload.get("status_code")
    status_msg = str(payload.get("status_msg") or "").strip()
    if status_code == 0 or status_code == "0" or status_msg == "视频已发布":
        post_id = str(payload.get("item_id") or "").strip()
        if not post_id and isinstance(payload.get("aweme"), dict):
            post_id = str(payload["aweme"].get("aweme_id") or "").strip()
        if not post_id:
            post_id = str(payload.get("postId") or "").strip()
        return True, post_id, ""
    reject_msg = status_msg or (f"status_code={status_code}" if status_code is not None else "")
    return False, "", reject_msg


async def _set_upload_files(page, media_type: str, want: List[str]) -> None:
    selectors = _IMAGE_INPUTS if media_type == "images" else _VIDEO_INPUTS
    last_error = None
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                await locator.set_input_files(want, timeout=15000)
                return
        except Exception as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    await page.locator('input[type="file"]').first.set_input_files(want, timeout=15000)


async def _apply_publish_settings(page, visibility: str, allow_save: bool) -> str:
    """设置「谁可以看」「保存权限」。公开 / 允许为抖音默认值；非默认失败时阻止提交。"""
    vis_label = {"friends": "好友可见", "private": "仅自己可见"}.get(visibility, "")
    if vis_label and not await _choose_radio(page, vis_label):
        return f"未能设置抖音可见范围「{vis_label}」，已停止提交以保留你的权限设置"
    if not allow_save and not await _choose_radio(page, "不允许"):
        return "未能设置抖音「不允许保存」，已停止提交以保留你的权限设置"
    return ""


async def publish_douyin(mgr: BrowserManager, identity: Identity,
                         storage_state_json: str, media_type: str, title: str,
                         desc: str, media_paths: List[str], topics: str = "",
                         visibility: str = "public", allow_save: bool = True,
                         headed: bool = True, timeout_seconds: int = 300,
                         on_submit: Callable[[], object | Awaitable[object]] | None = None,
                         on_stage: Callable[[str], object | Awaitable[object]] | None = None,
                         ) -> Tuple[bool, str, str]:
    """发布一条抖音作品。返回 (ok, result_url, error)。
    storage_state_json 仅用于校验(实际登录态在该账号持久 profile 里)。
    visibility: public 公开 | friends 好友可见 | private 仅自己可见;allow_save: 是否允许他人保存。"""
    files = [str(Path(p)) for p in media_paths if p and Path(p).exists()]
    if not files:
        return False, "", "没有可用的本地媒体文件(路径不存在)"
    tags = [t.strip().lstrip("#") for t in (topics or "").split(",") if t.strip()]
    # 抖音正文 = 简介 + 话题(话题写进正文,发布时自动识别 #)
    body = ((desc or "") + ("\n" + " ".join(f"#{t}" for t in tags) if tags else "")).strip()[:2000]

    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()

    # 保险:媒体上传我们全用 set_input_files(不触发 filechooser),故任何 filechooser
    # 事件都是意外(比如误点了「继续添加/封面」的 <input type=file>),自动取消避免卡死。
    # 注:抖音部分选图用系统级 showOpenFilePicker,此 hook 拦不到 —— 根本对策是别乱点。
    def _fc_guard(fc):
        async def _cancel():
            try:
                await fc.set_files([])
            except Exception:
                pass
        asyncio.ensure_future(_cancel())
    try:
        page.on("filechooser", _fc_guard)
    except Exception:
        pass

    submitted = {"clicked": False}
    create_state = {
        "seen": False,
        "accepted": False,
        "post_id": "",
        "rejected": "",
        "verify_hint": "",
    }

    async def on_response(response):
        try:
            if not submitted["clicked"]:
                return
            if str(getattr(response.request, "method", "") or "").upper() != "POST":
                return
            path = urlparse(str(response.url or "")).path.lower()
            if not any(seg in path for seg in (
                "/web/api/media/aweme/create",
                "/aweme/v1/creator/item/create",
                "/web/api/media/item/create",
            )):
                return
        except Exception:
            return
        try:
            hint = _parse_passport_decision(getattr(response, "headers", None) or {})
            if hint:
                create_state["seen"] = True
                create_state["verify_hint"] = hint
        except Exception:
            pass
        try:
            payload = await response.json()
        except Exception:
            if int(getattr(response, "status", 200) or 200) >= 400 and not create_state["verify_hint"]:
                create_state["seen"] = True
                create_state["rejected"] = f"HTTP {response.status}"
            return
        accepted, post_id, reject_msg = _parse_create_payload(payload)
        create_state["seen"] = True
        if accepted:
            create_state["accepted"] = True
            if post_id:
                create_state["post_id"] = post_id
        elif reject_msg:
            create_state["rejected"] = reject_msg

    try:
        page.on("response", on_response)
    except Exception:
        pass

    ok, result_url, error = False, "", ""
    try:
        url = IMAGE_URL if media_type == "images" else UPLOAD_URL
        _log(f"打开发布页 {url} (media_type={media_type}, files={len(files)})")
        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        await human_wait(page, 3000)
        if "passport" in page.url or "/login" in page.url:
            await _dump(page, "loggedout")
            return False, "", "logged_out:抖音创作平台未登录,请先在账号页点「创作者登录」"
        await _notify_stage(on_stage, "正在打开发布页并上传媒体…")
        if media_type == "images":
            await _click_first(page, _TAB_IMAGE, timeout=2000)
            await human_wait(page, 1200)
        try:
            await _set_upload_files(page, media_type, files if media_type == "images" else files[:1])
            _log("已提交文件,等待上传/转码…")
        except Exception as e:
            await _dump(page, "nofileinput")
            return False, "", f"上传文件失败(未找到文件输入框?): {e!r}"

        # 等表单编辑器挂载后先关闭引导气泡并填表，再确认上传进度条完成
        await _notify_stage(on_stage, "正在等待编辑表单加载…")
        edit_timeout = 120000 if media_type == "video" else 45000
        if not await _wait_editor(page, edit_timeout):
            await _dump(page, "noeditor")
            return False, "", ("上传后未进入编辑页(视频可能仍在转码或上传失败)。"
                               "请在弹出的窗口里查看,或到抖音创作平台重试。")
        _log(f"已进入编辑页 url={page.url}")
        await _notify_stage(on_stage, "正在填写作品标题、简介与发布设置…")
        await human_wait(page, 800)
        await _dismiss_guides(page)

        if title:
            await _fill_first(page, _TITLE_SEL, title.strip()[:30])
            await human_wait(page, 400)
        if body:
            if not await _fill_first(page, _DESC_SEL, body):
                await _dump(page, "nodesc")
                return False, "", "已进入发布页，但未能填入作品简介(选择器可能改版)"
        await human_wait(page, 800)

        # 发布设置:谁可以看 / 保存权限(非默认设置失败时阻止提交，防止隐私外泄)
        setting_err = await _apply_publish_settings(page, visibility, allow_save)
        if setting_err:
            await _dump(page, "settingsfail")
            return False, "", setting_err
        await human_wait(page, 600)

        upload_err = await _wait_upload_complete(page, edit_timeout, on_stage=on_stage)
        if upload_err:
            await _dump(page, "uploaderr")
            return False, "", upload_err

        await _dismiss_guides(page)
        await _notify_stage(on_stage, "正在准备自动点击「发布」…")
        # 轮询等「发布」按钮真正可用(排除 Semi 禁用态),最多 ~90s
        btn = None
        btn_polls = max(2, min(45, int(timeout_seconds)))
        for _ in range(btn_polls):
            btn = await _primary_publish_button(page)
            if btn is not None:
                break
            await human_wait(page, 2000)
        if btn is None:
            await _dump(page, "nobtn")
            return False, "", "未找到可用的发布按钮(可能仍在上传或发布页改版)。已存诊断截图到 data/debug/。"
        try:
            await btn.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        await _notify_submit(on_submit)
        submitted["clicked"] = True
        try:
            await DEFAULT_HUMAN_ENGINE.click_locator(btn, page=page, timeout=5000)
            _log("已点击发布按钮")
            await _notify_stage(on_stage, "已点击「发布」，等待抖音确认…")
        except Exception:
            await _dump(page, "clickfail")
            return False, "", ("write_uncertain:发布按钮点击状态未知(可能已被触发或被弹层遮挡)，"
                               "请先到抖音创作平台核对，系统不会自动重发。")

        # 成功判定 + 风控验证等待:必须依赖服务端 create_v2/item_id、页面真实跳转 /content/manage，
        # 或严格成功文案+管理页实查，绝不单靠页面宽泛文本("已发布/审核中")假阳性结束。
        ok = False
        deadline = float(max(1, int(timeout_seconds)))
        waited, verify_notified, toast_seen_at = 0.0, False, None
        while waited < deadline:
            if create_state["accepted"] and create_state["post_id"]:
                ok = True
                _log(f"命中抖音发布响应 postId={create_state['post_id']}")
                break
            if "/content/manage" in str(page.url or ""):
                # 留短窗口让同批 response.json() 完成读取，避免误判或丢失 postId
                await human_wait(page, 500)
                if not create_state["rejected"]:
                    ok = True
                    _log(f"页面已跳转管理页 url={page.url}")
                    break
            if create_state["verify_hint"] and not verify_notified:
                verify_notified = True
                await _dump(page, "verify")
                msg = (f"需要人工安全验证（{create_state['verify_hint']}），"
                       "请在弹出的浏览器窗口里完成短信验证码或原设备扫码")
                await _notify_stage(on_stage, msg)
                _log(f"⚠️ 抖音要求人工验证: {create_state['verify_hint']}")
            vkw = await _visible_any(page, _VERIFY_KW)
            if vkw and not verify_notified:
                verify_notified = True
                await _dump(page, "verify")
                await _notify_stage(
                    on_stage,
                    f"需要人工安全验证（页面提示「{vkw}」），请在弹出的浏览器窗口里完成短信验证码或原设备扫码",
                )
                _log(f"⚠️ 抖音要求人工验证「{vkw}」—— 请在弹出的浏览器窗口里完成"
                     f"(输入短信验证码 / 扫码 / 滑块),完成后本流程会自动继续,最多等 {int(deadline)}s")
            if create_state["rejected"] and not verify_notified and not create_state["verify_hint"]:
                await _dump(page, "rejected")
                error = (f"write_uncertain:抖音返回发布异常({create_state['rejected']})，"
                         "请到创作中心确认后再操作，系统不会自动重发")
                break
            blocked = await _visible_any(page, _BLOCKED_KW)
            if blocked and not verify_notified and not create_state["verify_hint"] and not create_state["accepted"]:
                png = await _dump(page, "blocked")
                error = (f"write_uncertain:抖音发布受阻（页面提示：{blocked}），"
                         f"请到抖音创作中心确认（系统不会自动重发）。"
                         f"{'诊断截图: ' + png if png else ''}")
                break
            hit = await _visible_any(page, _SUCCESS_KW)
            if hit and not create_state["rejected"]:
                if create_state["accepted"]:
                    ok = True
                    _log(f"命中成功文案「{hit}」且接口已确认")
                    break
                if toast_seen_at is None:
                    toast_seen_at = waited
                    _log(f"看到提示「{hit}」，继续等待跳转或 postId 确认…")
                elif waited - toast_seen_at >= 6:
                    if await _confirm_published_on_manage(page, title):
                        ok = True
                        _log(f"已在作品管理页核实标题「{title[:12]}」")
                    else:
                        png = await _dump(page, "unconfirmed")
                        error = ("write_uncertain:页面出现提示「" + hit
                                 + "」但未收到 postId 且作品管理未检出本次标题，"
                                 "请到抖音「作品管理」手动核对（系统不会自动重发）。"
                                 + (f"诊断截图: {png}" if png else ""))
                    break
            waited += await human_wait(page, 2000)
        if ok:
            if create_state["post_id"].isdigit() and visibility != "private":
                result_url = f"https://www.douyin.com/video/{create_state['post_id']}"
            elif "/content/manage" in str(page.url or ""):
                result_url = str(page.url)
            else:
                result_url = MANAGE_URL
            _log(f"发布成功 url={result_url}")
        elif not error and (verify_notified or create_state["verify_hint"]):
            hint = create_state["verify_hint"] or "短信验证码/扫码"
            error = (f"write_uncertain:发布被抖音风控拦下,需人工验证({hint}),"
                     "但在等待时间内未完成。请先到抖音创作平台核对是否已发布，系统不会自动重发。")
        elif not error:
            png = await _dump(page, "unconfirmed")
            error = ("write_uncertain:已点发布但未在页面确认到成功信号。请到抖音创作平台「作品管理」看是否已在列表"
                     "(视频常直接进审核中)；若确实没发出去,把 data/debug 里最新一张 "
                     f"dy_publish_unconfirmed_*.png {'('+png+') ' if png else ''}发我校准选择器。")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        try:
            await _dump(page, "exception")
        except Exception:
            pass
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
