"""哔哩哔哩创作中心视频投稿 (`member.bilibili.com`) 与图文动态 (`t.bilibili.com`) 浏览器发布器。

核心流程与安全边界：
- 监测 `preupload` 校验响应，遇到人机安全验证立即提示人工处理，不强行绕过；
- 通过 `.bcc-upload input` 提交本地视频并轮询 `.file-item-content-status-text` 上传完成状态；
- 支持双比例封面裁剪确认、必填创作声明选择、自制/转载切换与标签回车录入；
- 拦截 `POST /x/vu/web/add/v3` 合并标准化 `formData` 字段（分区/标签/互动开关/可见性/定时/话题）；
- 在实际提交请求发出时调用 `notify_submit`，提交后任何异常统一标记 `write_uncertain:`。
"""
from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from ...browser.identity import Identity
from ...browser.manager import BrowserManager
from ...media_tools import video_cover_and_meta
from .._submit import notify_stage as _notify_stage, notify_submit as _notify_submit
from .formatter import (
    CREATION_STATEMENT_OPTIONS,
    build_bili_form_from_task,
    format_bili_form,
)

VIDEO_UPLOAD_URL = (
    "https://member.bilibili.com/platform/upload/video/frame?page_from=creative_home_top_upload"
)
VIDEO_MANAGE_URL = "https://member.bilibili.com/platform/upload-manager/article"
DYNAMIC_PUBLISH_URL = "https://t.bilibili.com/"

_VIDEO_INPUT_SELECTORS = (
    ".bcc-upload input[type='file']",
    ".bcc-upload input",
    "input[type='file'][accept*='mp4']",
    "input[type='file'][accept*='video']",
    "input[type='file']",
)
_UPLOAD_STATUS_SELECTORS = (
    ".upload-queue .file-item .file-item-content-status-text",
    ".file-item-content-status-text",
    ".file-block-status-text",
    ".upload-status-intro",
)
_TITLE_SELECTORS = (
    ".video-title-content input",
    "input[placeholder*='标题']",
    "input[maxlength='80']",
)
_TAG_INPUT_SELECTORS = (
    ".tag-input-wrp input",
    "input[placeholder*='按回车键Enter创建标签']",
    "input[placeholder*='创建标签']",
    "input[placeholder*='标签']",
)
_DESC_SELECTORS = (
    ".archive-info-editor .ql-editor",
    ".ql-editor[contenteditable='true']",
    "textarea[placeholder*='简介']",
    "div[contenteditable='true'][placeholder*='简介']",
)
_SUBMIT_BUTTON_SELECTORS = (
    ".submit-container .submit-add",
    "span.submit-add",
    "button:has-text('立即投稿')",
    "div[role='button']:has-text('立即投稿')",
    "button:has-text('定时发布')",
)
_DISMISS_GUIDE_SELECTORS = (
    "button:has-text('我知道了')",
    "button:has-text('知道了')",
    "button:has-text('跳过')",
    ".guide-close",
)
_SUCCESS_KEYWORDS = ("稿件投递成功", "投稿成功", "已提交审核", "修改稿件")
_VERIFY_KEYWORDS = ("安全验证", "请完成验证", "拖动滑块", "极验", "验证码", "身份验证")
_UPLOAD_ERROR_KEYWORDS = ("上传失败", "转码失败", "文件损坏", "格式不支持", "超出大小限制")
_FORM_ERROR_KEYWORDS = (
    "请先上传封面",
    "请上传封面",
    "请选择创作声明",
    "请选择符合您视频内容的创作声明",
    "请选择分区",
    "请添加标签",
    "请填写标签",
    "请输入标题",
    "标题不能为空",
    "请填写转载来源",
)
_DEBUG_DIR = Path("./data/debug")


def _log(msg: str) -> None:
    print(f"[bili-publish] {msg}", flush=True)


async def _dump(page, tag: str) -> str:
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = _DEBUG_DIR / f"bili_publish_{tag}_{stamp}"
        png = str(base.with_suffix(".png"))
        try:
            await page.screenshot(path=png, full_page=True)
        except Exception:
            png = ""
        try:
            txt = await page.locator("body").inner_text()
        except Exception:
            txt = ""
        base.with_suffix(".txt").write_text(
            f"url: {page.url}\n\n{txt[:6000]}", encoding="utf-8"
        )
        return png
    except Exception as exc:
        _log(f"保存诊断快照失败: {exc!r}")
        return ""


async def _visible_text(page, keywords) -> str:
    for kw in keywords:
        try:
            loc = page.get_by_text(kw, exact=False)
            for idx in range(await loc.count()):
                if await loc.nth(idx).is_visible():
                    return kw
        except Exception:
            continue
    return ""


async def _dismiss_guides(page) -> None:
    for sel in _DISMISS_GUIDE_SELECTORS:
        try:
            loc = page.locator(sel)
            for idx in range(await loc.count()):
                cand = loc.nth(idx)
                if await cand.is_visible():
                    await cand.click(timeout=1500)
                    await page.wait_for_timeout(200)
        except Exception:
            continue


async def _fill_first_visible(page, selectors, text: str, timeout: int = 3500) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel)
            for idx in range(await loc.count()):
                cand = loc.nth(idx)
                if not await cand.is_visible():
                    continue
                await cand.click(timeout=timeout)
                try:
                    await cand.fill(text, timeout=timeout)
                except Exception:
                    await page.keyboard.press("Control+A")
                    await page.keyboard.type(text, delay=10)
                return True
        except Exception:
            continue
    return False


async def _ensure_dom_tags(page, tag_csv: str) -> None:
    """在页面标签输入框录入至少前几个标签，通过 B 站前端表单非空校验。"""
    tags = [t.strip() for t in (tag_csv or "").split(",") if t.strip()][:10]
    if not tags:
        return
    for sel in _TAG_INPUT_SELECTORS:
        try:
            loc = page.locator(sel)
            for idx in range(await loc.count()):
                inp = loc.nth(idx)
                if not await inp.is_visible():
                    continue
                for tag in tags[:5]:
                    await inp.click(timeout=2000)
                    await inp.fill(tag[:20], timeout=2000)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(150)
                return
        except Exception:
            continue


async def _page_has_cover(page, cover_state: Mapping[str, str]) -> bool:
    """检查页面是否已就绪稿件封面（捕获到 `/x/vu/web/cover/up` 或封面预览图已渲染）。"""
    if cover_state.get("url"):
        return True
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const empty = document.querySelector('.cover-main .cover-empty, .cover-empty');
                    if (empty && empty.offsetParent !== null) return false;
                    const selectors = [
                        '.cover-main .cover-img',
                        '.cover-preview img',
                        '.cover-upload img',
                        '.bcc-upload-cover img',
                        '[class*="cover"] img'
                    ];
                    for (const sel of selectors) {
                        for (const el of document.querySelectorAll(sel)) {
                            if (!el || el.offsetParent === null) continue;
                            const src = el.currentSrc || el.getAttribute('src') || '';
                            const bg = (window.getComputedStyle(el).backgroundImage || '');
                            if (
                                src.includes('hdslb.com') || src.includes('biliimg.com') || src.startsWith('data:image') || src.startsWith('blob:') ||
                                bg.includes('hdslb.com') || bg.includes('biliimg.com') || bg.includes('data:image')
                            ) {
                                return true;
                            }
                        }
                    }
                    return false;
                }"""
            )
        )
    except Exception:
        return False


async def _find_cover_file_input(page):
    """定位非视频类的图片封面 `<input type="file">`。"""
    for sel in (
        ".cover-editor .cover-editor-panel-select input[type='file']",
        ".cover-editor input[type='file']",
        ".bcc-dialog input[type='file']",
        ".cover-main input[type='file']",
        ".cover-upload input[type='file']",
        "input[type='file'][accept*='image']",
        "input[type='file'][accept*='png']",
        "input[type='file'][accept*='jpg']",
        "input[type='file'][accept*='jpeg']",
    ):
        try:
            loc = page.locator(sel)
            for idx in range(await loc.count()):
                cand = loc.nth(idx)
                accept = str(await cand.get_attribute("accept") or "").lower()
                if "mp4" in accept or "flv" in accept or "video" in accept:
                    continue
                return cand
        except Exception:
            continue
    return None


async def _upload_cover_via_api_and_bind_vue(page, cover_bytes: bytes) -> str:
    """回退方案：同源直传 `/x/vu/web/cover/up` 并尝试同步页面组件封面状态。"""
    b64_data = "data:image/jpeg;base64," + base64.b64encode(cover_bytes).decode("ascii")
    try:
        res = await page.evaluate(
            """async (dataUri) => {
                const m = document.cookie.match(/(?:^|;\\s*)bili_jct=([^;]+)/);
                const csrf = m ? decodeURIComponent(m[1]) : '';
                if (!csrf) return { url: '', err: 'no_csrf' };
                const body = 'cover=' + encodeURIComponent(dataUri);
                const resp = await fetch(
                    'https://member.bilibili.com/x/vu/web/cover/up?t=' + Date.now() + '&csrf=' + encodeURIComponent(csrf),
                    {
                        method: 'POST',
                        credentials: 'include',
                        headers: { 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8' },
                        body
                    }
                );
                const json = await resp.json();
                const url = (json && json.code === 0 && json.data && json.data.url) ? String(json.data.url) : '';
                if (!url) return { url: '', err: (json && json.message) || 'cover_up_failed' };

                // 尝试同步写入 Vue 2 / Vue 3 组件实例中的 cover 字段，消除前端“请先上传封面”拦截
                const visited = new Set();
                const patchObj = (obj, depth = 0) => {
                    if (!obj || typeof obj !== 'object' || depth > 5 || visited.has(obj)) return;
                    visited.add(obj);
                    try {
                        if ('cover' in obj && ('title' in obj || 'tid' in obj || 'videos' in obj || 'copyright' in obj)) {
                            if (!obj.cover) obj.cover = url;
                        }
                        for (const k of ['archive', 'form', 'formData', 'videoInfo', 'submitData', 'modelValue']) {
                            if (obj[k] && typeof obj[k] === 'object') patchObj(obj[k], depth + 1);
                        }
                    } catch (_) {}
                };
                for (const el of document.querySelectorAll('*')) {
                    if (el.__vue__) {
                        patchObj(el.__vue__.$data || el.__vue__);
                    }
                    if (el.__vueParentComponent) {
                        const c = el.__vueParentComponent;
                        patchObj(c.setupState);
                        patchObj(c.data);
                        patchObj(c.ctx);
                    }
                }
                return { url, err: '' };
            }""",
            b64_data,
        )
        if isinstance(res, Mapping) and res.get("url"):
            return str(res["url"])
    except Exception as exc:
        _log(f"API 直传封面回退异常: {exc!r}")
    return ""


async def _close_residual_cover_modals(page) -> None:
    """兜底关闭未退出的封面裁剪/同步弹窗，防止遮挡后续标题与声明输入。"""
    for _ in range(3):
        try:
            has_modal = await page.evaluate(
                """() => {
                    const m1 = document.querySelector('.videoup-confirm-modal');
                    const m2 = document.querySelector('.cover-editor-button');
                    return !!((m1 && m1.offsetParent !== null) || (m2 && m2.offsetParent !== null));
                }"""
            )
            if not has_modal:
                return
            cancel_loc = page.locator(
                ".cover-editor-button :text-is('取消'), .videoup-confirm-modal :text-is('取消')"
            )
            for idx in range(await cancel_loc.count()):
                btn = cancel_loc.nth(idx)
                if await btn.is_visible():
                    await btn.click(timeout=1500)
                    await page.wait_for_timeout(300)
                    break
            else:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
        except Exception:
            return


async def _apply_custom_cover(page, cover_file: str, cover_state: Dict[str, str]) -> bool:
    """上传封面并完成 B 站双比例确认流程（`完成` -> `确认同步` -> `完成`），全程拦截系统文件对话框。"""
    if not cover_file or not Path(cover_file).exists():
        return False

    # 全局拦截 filechooser 事件，彻底杜绝任何点击弹出 Windows 原生「打开」文件窗口
    fc_handled = {"count": 0}

    def _on_unexpected_fc(fc):
        fc_handled["count"] += 1
        asyncio.create_task(fc.set_files(cover_file))

    page.on("filechooser", _on_unexpected_fc)
    try:
        await page.evaluate("() => window.scrollTo(0, 0)")
        await page.wait_for_timeout(250)

        opened = False
        for trigger_sel in (
            ".cover-main .cover-empty",
            ".cover-main .cover-img",
            ".cover-slot",
            ".cover-upload",
            ".bcc-upload-cover",
            ":text-is('添加封面')",
            ":text-is('更改封面')",
            ":text-is('封面设置')",
        ):
            try:
                loc = page.locator(trigger_sel)
                for idx in range(await loc.count()):
                    cand = loc.nth(idx)
                    if await cand.is_visible():
                        await cand.scroll_into_view_if_needed(timeout=2000)
                        await cand.click(timeout=3000)
                        opened = True
                        break
                if opened:
                    break
            except Exception:
                continue

        await page.wait_for_timeout(700)

        uploaded_to_cropper = fc_handled["count"] > 0
        if not uploaded_to_cropper:
            # 优先通过拦截「上传封面」触发的 FileChooser 注入图片（触发 B 站完整裁剪器状态且不弹系统框）
            for up_sel in (
                ".cover-editor-panel-select-item",
                ".cover-upload",
                ":text-is('上传封面')",
                ":text-is('本地上传')",
            ):
                try:
                    uloc = page.locator(up_sel)
                    target_btn = None
                    for idx in range(await uloc.count()):
                        cand = uloc.nth(idx)
                        if await cand.is_visible():
                            target_btn = cand
                            break
                    if target_btn is None:
                        continue
                    prev_count = fc_handled["count"]
                    await target_btn.click(timeout=2500)
                    for _ in range(10):
                        if fc_handled["count"] > prev_count:
                            uploaded_to_cropper = True
                            break
                        await page.wait_for_timeout(150)
                    if uploaded_to_cropper:
                        break
                except Exception:
                    continue

        if not uploaded_to_cropper:
            file_inp = await _find_cover_file_input(page)
            if file_inp is not None:
                await file_inp.set_input_files(cover_file, timeout=10000)
                uploaded_to_cropper = True

        if uploaded_to_cropper:
            await page.wait_for_timeout(1200)
            # 依次推进「完成（4:3） -> 确认同步 -> 完成（16:9）」，直到双比例封面上传完毕且弹窗关闭
            for _ in range(16):
                state = await page.evaluate(
                    """() => {
                        const syncModal = document.querySelector('.videoup-confirm-modal');
                        const editor = document.querySelector('.cover-editor-button');
                        const visTexts = Array.from(document.querySelectorAll('button, div.button, span'))
                            .filter(e => e.offsetParent !== null && e.children.length === 0)
                            .map(e => (e.innerText || '').trim())
                            .filter(t => ['确认同步', '不同步，手动编辑', '完成', '确定'].includes(t));
                        return {
                            syncVis: !!(syncModal && syncModal.offsetParent !== null),
                            editorVis: !!(editor && editor.offsetParent !== null),
                            visTexts,
                        };
                    }"""
                )
                if not state["syncVis"] and not state["editorVis"] and await _page_has_cover(page, cover_state):
                    return True
                if "确认同步" in state["visTexts"]:
                    sync_btn = page.get_by_text("确认同步", exact=True)
                    for idx in range(await sync_btn.count()):
                        if await sync_btn.nth(idx).is_visible():
                            await sync_btn.nth(idx).click(timeout=2500)
                            break
                elif "完成" in state["visTexts"]:
                    done_loc = page.locator(
                        ".cover-editor-button .submit, div.submit:has-text('完成'), button:has-text('完成')"
                    )
                    for idx in range(await done_loc.count()):
                        if await done_loc.nth(idx).is_visible():
                            await done_loc.nth(idx).click(timeout=2500)
                            break
                await page.wait_for_timeout(750)

            if await _page_has_cover(page, cover_state):
                return True
    except Exception as exc:
        _log(f"DOM 上传封面未完全命中，尝试兜底方案: {exc!r}")
    finally:
        try:
            page.remove_listener("filechooser", _on_unexpected_fc)
        except Exception:
            pass
        await _close_residual_cover_modals(page)

    try:
        raw_bytes = Path(cover_file).read_bytes()
        api_url = await _upload_cover_via_api_and_bind_vue(page, raw_bytes)
        if api_url:
            cover_state["url"] = api_url
            _log(f"已通过同源 API 上传封面: {api_url}")
            return True
    except Exception as exc:
        _log(f"读取封面文件回退上传失败: {exc!r}")
    return False


async def _ensure_bili_cover(
    page,
    *,
    video_file: str,
    cover_candidate: str,
    cover_state: Dict[str, str],
    on_stage=None,
) -> None:
    """确保视频稿件具备有效封面（支持自定义封面、内置 FFmpeg 自动抽帧封面及页面推荐封面兜底）。"""
    if cover_candidate and Path(cover_candidate).exists():
        await _notify_stage(on_stage, "正在上传自定义视频封面…")
        await _apply_custom_cover(page, cover_candidate, cover_state)
        return

    for _ in range(4):
        if await _page_has_cover(page, cover_state):
            return
        await page.wait_for_timeout(500)

    await _notify_stage(on_stage, "正在从视频自动提取封面并上传…")
    jpeg_bytes = b""
    try:
        jpeg_bytes, _meta = await asyncio.to_thread(video_cover_and_meta, video_file)
    except Exception as exc:
        _log(f"FFmpeg 自动提取视频首帧封面失败: {exc!r}")

    if jpeg_bytes:
        with tempfile.TemporaryDirectory(prefix="bili-cover-") as tmp_dir:
            auto_cover = Path(tmp_dir) / "cover.jpg"
            auto_cover.write_bytes(jpeg_bytes)
            if await _apply_custom_cover(page, str(auto_cover), cover_state):
                return

    # 兜底：若页面已生成系统推荐封面缩略图，直接点选第一张非 AI 推荐帧
    try:
        rec_cards = page.locator(".cover-recommend-list .img-item-cover")
        for idx in range(await rec_cards.count()):
            card = rec_cards.nth(idx)
            if await card.is_visible():
                await card.click(timeout=2000)
                await page.wait_for_timeout(800)
                if await _page_has_cover(page, cover_state):
                    return
    except Exception:
        pass


def _resolve_creation_statement_id(opts: Mapping[str, Any], copyright_type: int) -> int:
    """将表单 `creation_statement_id` / 旧版 `declaration_index` 归一化为 B 站创作声明枚举值。"""
    if int(copyright_type or 1) == 2:
        return -2
    valid_values = {-1, 1, 2, 3, 4, -2}
    raw_csid = opts.get("creation_statement_id")
    if raw_csid is not None and str(raw_csid).strip() != "":
        try:
            csid = int(raw_csid)
            if csid in valid_values:
                return csid
        except (TypeError, ValueError):
            pass
    raw_decl = opts.get("declaration_index", opts.get("statement"))
    if raw_decl is not None and str(raw_decl).strip() != "":
        try:
            dval = int(raw_decl)
            if dval in {-1, -2}:
                return dval
            legacy_map = {0: -1, 1: 2, 2: 4, 3: 1, 4: 4, 5: 4}
            return legacy_map.get(dval, -1)
        except (TypeError, ValueError):
            pass
    return -1


async def _apply_creation_statement(page, statement_id: int) -> None:
    """选择 B 站必填项「* 创作声明」（默认 `-1` 即「内容无需标注」）。"""
    sid = int(statement_id if statement_id is not None else -1)
    if sid == 0:
        sid = -1
    label = next(
        (str(it.get("label") or "") for it in CREATION_STATEMENT_OPTIONS if it.get("value") == sid),
        "内容无需标注",
    )
    try:
        wrap = page.locator(".creation-statement-container .bcc-select-input-wrap").first
        if not await wrap.count() or not await wrap.is_visible():
            return
        await wrap.scroll_into_view_if_needed(timeout=2000)
        await wrap.click(timeout=2500)
        await page.wait_for_timeout(350)
        opts = page.locator(
            ".creation-statement-container .bcc-select-option-list .bcc-option, "
            ".bcc-select-option-list .bcc-option, .bcc-option"
        )
        for idx in range(await opts.count()):
            opt = opts.nth(idx)
            if not await opt.is_visible():
                continue
            txt = ((await opt.inner_text()) or "").strip()
            if label in txt:
                await opt.click(timeout=2500)
                await page.wait_for_timeout(250)
                return
    except Exception as exc:
        _log(f"设置创作声明未命中(不阻断发布): {exc!r}")


def _merge_add_v3_payload(
    raw_post_data: Optional[str],
    form_data: Mapping[str, Any],
    *,
    explicit_tid: bool,
    cover_url: str = "",
) -> Optional[str]:
    """将标准化 `formData` 合并进页面发往 `/x/vu/web/add/v3` 的 JSON 请求体。"""
    if not raw_post_data:
        return None
    try:
        payload = json.loads(raw_post_data)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    merged = dict(payload)
    merged["title"] = form_data["title"]
    if isinstance(merged.get("videos"), list) and len(merged["videos"]) == 1 and isinstance(merged["videos"][0], dict):
        merged["videos"] = [{**merged["videos"][0], "title": form_data["title"]}]
    merged["desc"] = form_data["desc"]
    merged["desc_format_id"] = 9999
    if cover_url and not str(merged.get("cover") or "").strip():
        merged["cover"] = cover_url
    if form_data.get("tag"):
        merged["tag"] = form_data["tag"]

    section_id = int(form_data.get("human_type2") or form_data.get("tid") or 0)
    if explicit_tid and section_id > 0:
        if section_id >= 1000 or "human_type2" in merged:
            merged["human_type2"] = section_id
        if section_id < 1000:
            merged["tid"] = section_id
        elif not int(merged.get("tid") or 0):
            merged["tid"] = 21
    elif not int(merged.get("tid") or 0):
        merged["tid"] = section_id if (0 < section_id < 1000) else 21

    if int(form_data["copyright"]) == 2:
        merged["copyright"] = 2
        merged["source"] = str(form_data.get("source") or "")
        merged.pop("no_reprint", None)
    else:
        # B 站新版前端搭配 creation_statement 时发送 copyright=3，切勿覆盖回 1
        if int(payload.get("copyright") or 0) != 3:
            merged["copyright"] = int(form_data["copyright"])
        merged["no_reprint"] = int(form_data.get("no_reprint", 1))
        merged.pop("source", None)

    for key in (
        "recreate",
        "up_close_reply",
        "up_close_danmu",
        "up_selection_reply",
        "is_only_self",
        "dolby",
        "lossless_music",
    ):
        if key in form_data:
            merged[key] = form_data[key]

    if form_data.get("dynamic"):
        merged["dynamic"] = form_data["dynamic"]
    if form_data.get("topic_id"):
        merged["topic_id"] = form_data["topic_id"]
        if form_data.get("topic_detail"):
            merged["topic_detail"] = form_data["topic_detail"]
        if form_data.get("mission_id"):
            merged["mission_id"] = form_data["mission_id"]

    now_sec = int(time.time())
    dtime = int(form_data.get("dtime") or 0)
    if dtime > now_sec + 7500:
        merged["dtime"] = dtime
    else:
        merged.pop("dtime", None)

    return json.dumps(merged, ensure_ascii=False, separators=(",", ":"))


async def _wait_bili_video_uploaded(
    page,
    *,
    timeout_ms: int,
    preupload_state: Dict[str, str],
    on_stage=None,
) -> str:
    """轮询 `.file-item-content-status-text` 直到视频分片上传完成。"""
    deadline = asyncio.get_running_loop().time() + max(15.0, timeout_ms / 1000.0)
    last_status = ""
    saw_progress = False
    while asyncio.get_running_loop().time() < deadline:
        if preupload_state.get("error"):
            return preupload_state["error"]
        err_kw = await _visible_text(page, _UPLOAD_ERROR_KEYWORDS)
        if err_kw:
            return f"B 站视频上传失败: {err_kw}"

        status_text = ""
        for sel in _UPLOAD_STATUS_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    status_text = ((await loc.inner_text()) or "").strip()
                    if status_text:
                        break
            except Exception:
                continue

        if status_text:
            saw_progress = True
            if status_text != last_status:
                last_status = status_text
                await _notify_stage(on_stage, f"正在上传 B 站视频（{status_text}）…")
            if any(done_kw in status_text for done_kw in ("上传完成", "Complete")):
                if "上传中" not in status_text and "暂停" not in status_text:
                    return ""
            if "失败" in status_text or "错误" in status_text:
                return f"B 站视频上传异常: {status_text}"
        elif saw_progress:
            # 上传状态条收起且标题编辑框已就绪
            for tsel in _TITLE_SELECTORS:
                try:
                    if await page.locator(tsel).count():
                        return ""
                except Exception:
                    continue

        await page.wait_for_timeout(1200)
    return "B 站视频上传超时，请检查网络或视频大小后重试"


async def _find_submit_button(page):
    for sel in _SUBMIT_BUTTON_SELECTORS:
        try:
            loc = page.locator(sel)
            for idx in range(await loc.count()):
                cand = loc.nth(idx)
                if not await cand.is_visible():
                    continue
                cls = str(await cand.get_attribute("class") or "").lower()
                aria_dis = await cand.get_attribute("aria-disabled")
                if "disabled" in cls or aria_dis == "true":
                    continue
                return cand
        except Exception:
            continue
    return None


async def _publish_bilibili_dynamic_images(
    mgr: BrowserManager,
    identity: Identity,
    *,
    title: str,
    desc: str,
    files: List[str],
    topics: str,
    timeout_seconds: int,
    on_submit=None,
    on_stage=None,
) -> Tuple[bool, str, str]:
    """在 `t.bilibili.com` 发布 B 站图文动态。"""
    tags = [t.strip().lstrip("#").rstrip("#") for t in (topics or "").split(",") if t.strip()]
    tag_line = " ".join(f"#{t}#" for t in tags[:8])
    body_parts = [p.strip() for p in (title, desc, tag_line) if p and p.strip()]
    body_text = "\n".join(body_parts)[:1000] or "分享图片"

    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    ok, result_url, error = False, "", ""
    submitted = {"clicked": False}
    dyn_resp = {"seen": False, "ok": False, "url": "", "message": ""}

    async def on_response(response):
        if dyn_resp["seen"] or not submitted["clicked"]:
            return
        if str(getattr(response.request, "method", "") or "").upper() != "POST":
            return
        url_l = str(response.url or "").lower()
        if "/x/dynamic/feed/create/dyn" not in url_l and "dynamic_svr/create" not in url_l:
            return
        try:
            payload = await response.json()
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        dyn_resp["seen"] = True
        code = payload.get("code")
        dyn_resp["ok"] = code in (0, "0")
        dyn_resp["message"] = str(payload.get("message") or f"code={code}")
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        dyn_id = str(data.get("dyn_id_str") or data.get("dynamic_id_str") or "")
        if dyn_resp["ok"] and dyn_id:
            dyn_resp["url"] = f"https://www.bilibili.com/opus/{dyn_id}"

    page.on("response", on_response)
    try:
        await _notify_stage(on_stage, "正在打开 B 站动态页…")
        await page.goto(DYNAMIC_PUBLISH_URL, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(3000)
        body_dom = ""
        try:
            body_dom = await page.locator("body").inner_text()
        except Exception:
            pass
        if "passport.bilibili.com" in page.url or "/login" in page.url or "立即登录" in body_dom[:400]:
            await _dump(page, "dyn-loggedout")
            return False, "", "logged_out:哔哩哔哩账号未登录，请先在账号页扫码或网页登录"

        await _notify_stage(on_stage, "正在填写动态正文并上传图片…")
        filled = await _fill_first_visible(
            page,
            (
                ".bili-rich-textarea__inner",
                "div[contenteditable='true']",
                "textarea",
            ),
            body_text,
        )
        if not filled:
            await _dump(page, "dyn-no-editor")
            return False, "", "未找到 B 站动态正文输入框"

        uploaded = False
        for img_sel in (
            ".bili-pics-uploader input[type='file']",
            "input[type='file'][accept*='image']",
            "input[type='file']",
        ):
            try:
                loc = page.locator(img_sel).first
                if await loc.count():
                    await loc.set_input_files(files[:9], timeout=15000)
                    uploaded = True
                    break
            except Exception:
                continue
        if not uploaded:
            try:
                icon = page.locator(".bili-dyn-publishing__image-upload, [title*='图片']").first
                if await icon.count():
                    await icon.click(timeout=3000)
                    await page.wait_for_timeout(500)
                    loc = page.locator("input[type='file']").first
                    if await loc.count():
                        await loc.set_input_files(files[:9], timeout=15000)
                        uploaded = True
            except Exception:
                pass
        if not uploaded:
            await _dump(page, "dyn-no-upload")
            return False, "", "未找到 B 站动态图片上传入口"

        await page.wait_for_timeout(3500)
        pub_btn = None
        for bsel in (
            ".bili-dyn-publishing__action.launcher",
            "div.bili-dyn-publishing__action:has-text('发布')",
            "button:has-text('发布')",
        ):
            loc = page.locator(bsel)
            for idx in range(await loc.count()):
                cand = loc.nth(idx)
                if await cand.is_visible():
                    cls = str(await cand.get_attribute("class") or "").lower()
                    if "disabled" not in cls:
                        pub_btn = cand
                        break
            if pub_btn is not None:
                break

        if pub_btn is None:
            await _dump(page, "dyn-no-btn")
            return False, "", "未找到可用的 B 站动态「发布」按钮"

        await _notify_submit(on_submit)
        submitted["clicked"] = True
        await pub_btn.click(timeout=5000)
        await _notify_stage(on_stage, "已点击动态「发布」，等待 B 站确认…")

        for _ in range(max(10, int(timeout_seconds))):
            if dyn_resp["seen"]:
                if dyn_resp["ok"]:
                    ok = True
                    result_url = dyn_resp["url"] or DYNAMIC_PUBLISH_URL
                else:
                    error = (
                        f"write_uncertain:B 站动态返回错误({dyn_resp['message']})，"
                        "请先在动态主页核对是否已发出"
                    )
                break
            await page.wait_for_timeout(1000)

        if not ok and not error:
            png = await _dump(page, "dyn-unconfirmed")
            error = (
                "write_uncertain:已点击动态发布但未收到确认回包，请到 t.bilibili.com 核对"
                f"（快照: {png or 'data/debug'}）"
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        prefix = "write_uncertain:" if submitted["clicked"] else ""
        error = f"{prefix}B 站动态发布异常: {exc!r}"
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


async def publish_bilibili(
    mgr: BrowserManager,
    identity: Identity,
    storage_state_json: str,
    media_type: str,
    title: str,
    desc: str,
    media_paths: List[str],
    *,
    topics: str = "",
    visibility: str = "public",
    allow_comment: bool = True,
    cover_path: str = "",
    bili_options: Optional[Mapping[str, Any]] = None,
    headed: bool = True,
    timeout_seconds: int = 240,
    on_submit: Callable[[], object | Awaitable[object]] | None = None,
    on_stage: Callable[[str], object | Awaitable[object]] | None = None,
    **_unused_kwargs: Any,
) -> Tuple[bool, str, str]:
    """发布一条哔哩哔哩视频稿件或图文动态，返回 ``(ok, result_url, error)``。"""
    _ = storage_state_json, headed
    files = [str(Path(p).resolve()) for p in (media_paths or []) if p and Path(p).exists()]
    if not files:
        return False, "", "没有可用的本地媒体文件(路径不存在)"

    if media_type == "images":
        return await _publish_bilibili_dynamic_images(
            mgr,
            identity,
            title=title,
            desc=desc,
            files=files,
            topics=topics,
            timeout_seconds=timeout_seconds,
            on_submit=on_submit,
            on_stage=on_stage,
        )

    opts = dict(bili_options or {})
    if not allow_comment and "up_close_reply" not in opts:
        opts["up_close_reply"] = True
    if opts.get("closeReply") and "up_close_reply" not in opts:
        opts["up_close_reply"] = True
    if opts.get("closeDanmu") and "up_close_danmu" not in opts:
        opts["up_close_danmu"] = True
    if opts.get("selectionReply") and "up_selection_reply" not in opts:
        opts["up_selection_reply"] = True
    explicit_tid = bool(opts.get("tid") or opts.get("human_type2"))
    if not explicit_tid:
        opts["tid"] = 21  # 默认分区：日常(21)，页面自动推荐有值时保持页面推荐
    tag_list = [t.strip() for t in (topics or "").split(",") if t.strip()]
    if isinstance(opts.get("tags"), list):
        tag_list = [str(t).strip() for t in opts["tags"] if str(t).strip()] or tag_list

    try:
        raw_form = build_bili_form_from_task(
            title=title,
            desc=desc,
            tags=tag_list,
            bili_options=opts,
        )
        built = format_bili_form(raw_form)
    except ValueError as exc:
        return False, "", f"B 站投稿表单参数校验未通过: {exc}"
    form_data = dict(built["formData"])
    form_data["is_only_self"] = (
        1 if (visibility == "private" or opts.get("is_only_self")) else 0
    )

    cover_candidate = str(cover_path or opts.get("cover") or "").strip()
    if not cover_candidate and len(files) >= 2:
        ext2 = Path(files[1]).suffix.lower()
        if ext2 in {".jpg", ".jpeg", ".png", ".webp"}:
            cover_candidate = files[1]

    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    ok, result_url, error = False, "", ""
    submitted = {"clicked": False, "sent": False}
    preupload_state: Dict[str, str] = {"error": ""}
    cover_state: Dict[str, str] = {"url": ""}
    publish_response: Dict[str, Any] = {"seen": False, "ok": False, "message": "", "url": ""}

    async def handle_add_v3(route):
        req = route.request
        if str(req.method or "").upper() == "POST":
            if not submitted["sent"]:
                submitted["sent"] = True
                await _notify_submit(on_submit)
            merged_body = _merge_add_v3_payload(
                req.post_data,
                form_data,
                explicit_tid=explicit_tid,
                cover_url=cover_state.get("url", ""),
            )
            if merged_body is not None:
                await route.continue_(post_data=merged_body)
                return
        await route.continue_()

    async def on_response(response):
        url_str = str(response.url or "")
        path_l = urlparse(url_str).path.lower()
        if "/x/vu/web/cover/up" in path_l:
            try:
                cov_json = await response.json()
                if isinstance(cov_json, Mapping) and cov_json.get("code") in (0, "0"):
                    cdata = cov_json.get("data") if isinstance(cov_json.get("data"), Mapping) else {}
                    if cdata.get("url"):
                        cover_state["url"] = str(cdata["url"])
            except Exception:
                pass
            return

        if "preupload" in path_l:
            try:
                pre_json = await response.json()
                if (
                    isinstance(pre_json, Mapping)
                    and int(pre_json.get("code") or 0) > 0
                    and pre_json.get("message")
                ):
                    preupload_state["error"] = (
                        f"{pre_json['message']}: 请用官方页面发布，完成验证后再操作"
                    )
            except Exception:
                pass
            return

        if publish_response["seen"] or not (submitted["clicked"] or submitted["sent"]):
            return
        if str(getattr(response.request, "method", "") or "").upper() != "POST":
            return
        if "/x/vu/web/add" not in path_l:
            return
        status = int(getattr(response, "status", 200) or 200)
        if status >= 500:
            publish_response["seen"] = True
            publish_response["ok"] = False
            publish_response["message"] = f"HTTP {status}"
            return
        try:
            payload = await response.json()
        except Exception:
            return
        if not isinstance(payload, Mapping):
            return
        publish_response["seen"] = True
        code = payload.get("code")
        publish_response["ok"] = code in (0, "0")
        publish_response["message"] = str(payload.get("message") or f"code={code}")
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        bvid = str(data.get("bvid") or "").strip()
        if publish_response["ok"]:
            publish_response["url"] = (
                f"https://www.bilibili.com/video/{bvid}" if bvid else VIDEO_MANAGE_URL
            )

    await page.route("**/x/vu/web/add**", handle_add_v3)
    page.on("response", on_response)

    try:
        _log(f"打开创作中心视频投稿页 video={files[0]}")
        await _notify_stage(on_stage, "正在打开 B 站创作中心投稿页…")
        await page.goto(VIDEO_UPLOAD_URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3500)
        body_text = ""
        try:
            body_text = await page.locator("body").inner_text()
        except Exception:
            pass
        if (
            "passport.bilibili.com" in page.url
            or "/login" in page.url
            or "账密登录" in body_text
            or "扫码登录" in body_text
        ):
            await _dump(page, "loggedout")
            return False, "", "logged_out:哔哩哔哩创作中心未登录，请先在账号页登录"

        await _dismiss_guides(page)

        uploaded = False
        for sel in _VIDEO_INPUT_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    await loc.set_input_files(files[0], timeout=20000)
                    uploaded = True
                    break
            except Exception:
                continue
        if not uploaded:
            await _dump(page, "no-video-input")
            return False, "", "未找到 B 站创作中心视频上传输入框(.bcc-upload input)"

        await _notify_stage(on_stage, "正在上传视频分片并等待编辑表单…")
        up_err = await _wait_bili_video_uploaded(
            page,
            timeout_ms=max(120_000, int(timeout_seconds) * 1000),
            preupload_state=preupload_state,
            on_stage=on_stage,
        )
        if up_err:
            await _dump(page, "upload-error")
            return False, "", up_err

        await _dismiss_guides(page)
        await _ensure_bili_cover(
            page,
            video_file=files[0],
            cover_candidate=cover_candidate,
            cover_state=cover_state,
            on_stage=on_stage,
        )
        await _notify_stage(on_stage, "正在填写 B 站稿件标题、标签与声明…")

        await _fill_first_visible(page, _TITLE_SELECTORS, form_data["title"])
        if form_data.get("desc"):
            await _fill_first_visible(page, _DESC_SELECTORS, form_data["desc"])

        copyright_num = int(form_data.get("copyright") or 1)
        statement_id = _resolve_creation_statement_id(opts, copyright_num)
        await _apply_creation_statement(page, statement_id)

        if copyright_num == 2:
            try:
                repost_radio = page.get_by_text("转载", exact=True).first
                if await repost_radio.count() and await repost_radio.is_visible():
                    await repost_radio.click(timeout=2000)
                    await page.wait_for_timeout(200)
                await _fill_first_visible(
                    page,
                    (
                        "input[placeholder*='转载']",
                        "input[placeholder*='来源']",
                        "input[placeholder*='出处']",
                        "input[placeholder*='链接']",
                    ),
                    str(form_data.get("source") or ""),
                )
            except Exception:
                pass

        await _ensure_dom_tags(page, form_data.get("tag", ""))

        await _dismiss_guides(page)
        await _notify_stage(on_stage, "正在准备点击「立即投稿」…")
        submit_btn = None
        for _ in range(45):
            submit_btn = await _find_submit_button(page)
            if submit_btn is not None:
                break
            await page.wait_for_timeout(1000)
        if submit_btn is None:
            await _dump(page, "no-submit-btn")
            return False, "", "未找到可用的「立即投稿」按钮（已保存 data/debug 诊断快照）"

        await submit_btn.scroll_into_view_if_needed(timeout=3000)
        submitted["clicked"] = True
        try:
            await submit_btn.click(timeout=5000)
        except Exception:
            await page.evaluate("() => document.querySelector('.submit-container .submit-add')?.click()")
        _log("已点击立即投稿，等待 add/v3 请求与响应")
        await _notify_stage(on_stage, "已点击「立即投稿」，等待 B 站确认…")

        waited = 0
        verify_notified = False
        retried_cover = False
        deadline = max(20, int(timeout_seconds))
        while waited < deadline:
            if publish_response["seen"]:
                if publish_response["ok"]:
                    ok = True
                    result_url = publish_response["url"] or VIDEO_MANAGE_URL
                    break
                error = (
                    f"write_uncertain:B 站返回投稿拒绝({publish_response['message']})，"
                    "请先到创作中心核对是否已生成稿件（系统不会自动重发）"
                )
                break

            succ_kw = await _visible_text(page, _SUCCESS_KEYWORDS)
            if succ_kw:
                ok = True
                result_url = publish_response["url"] or VIDEO_MANAGE_URL
                break

            if not submitted["sent"]:
                form_err_kw = await _visible_text(page, _FORM_ERROR_KEYWORDS)
                if form_err_kw:
                    if "封面" in form_err_kw and not retried_cover:
                        retried_cover = True
                        _log("检测到「请先上传封面」提示，立即自动补传视频首帧封面并重试点击")
                        await _ensure_bili_cover(
                            page,
                            video_file=files[0],
                            cover_candidate=cover_candidate,
                            cover_state=cover_state,
                            on_stage=on_stage,
                        )
                        await page.wait_for_timeout(800)
                        btn_retry = await _find_submit_button(page)
                        if btn_retry is not None:
                            await btn_retry.scroll_into_view_if_needed(timeout=3000)
                            await btn_retry.click(timeout=5000)
                        else:
                            await page.evaluate(
                                "() => document.querySelector('.submit-container .submit-add')?.click()"
                            )
                        waited += 1
                        continue
                    png = await _dump(page, "form-rejected")
                    return (
                        False,
                        "",
                        f"B 站投稿页面校验未通过（{form_err_kw}），请求尚未发出，可直接修改后重试"
                        f"（快照: {png or 'data/debug'}）",
                    )

            verify_kw = await _visible_text(page, _VERIFY_KEYWORDS)
            if verify_kw and not verify_notified:
                verify_notified = True
                await _notify_stage(
                    on_stage,
                    f"需要人工安全验证（页面提示「{verify_kw}」），请在弹出的 B 站窗口完成验证",
                )
                _log(f"检测到人机验证提示：{verify_kw}，等待人工操作")

            await page.wait_for_timeout(1000)
            waited += 1

        if not ok and not error:
            png = await _dump(page, "unconfirmed")
            if submitted["sent"]:
                error = (
                    "write_uncertain:已发出投稿请求但未收到成功回包；请先到 B 站创作中心核对稿件状态"
                    f"（系统不会自动重发）。诊断截图: {png or 'data/debug'}"
                )
            else:
                error = (
                    "点击立即投稿后页面未发出提交请求（可能仍有必填项提示未通过），可检查后重试"
                    f"（诊断截图: {png or 'data/debug'}）"
                )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            await _dump(page, "exception")
        except Exception:
            pass
        prefix = "write_uncertain:" if submitted["sent"] else ""
        error = f"{prefix}B 站发布异常: {exc!r}"
    finally:
        try:
            await page.unroute("**/x/vu/web/add**", handle_add_v3)
        except Exception:
            pass
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        try:
            await ctx.close()
        except Exception:
            pass
    return ok, result_url, error
