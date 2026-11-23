"""Instagram 多模式发布器（移动端 API 分阶段上传 + 网页端创作弹窗 RPA 双通道）。

支持能力：
- 单图帖子 (`post`) / 图片快拍 (`story`)
- 单视频 Reels (`reel`) / 视频帖子 (`post`) / 视频快拍 (`story`)
- 多图及图文混排轮播 (`carousel` / `configure_sidecar`, 2~10 项)

安全不变量：
- 所有写提交前调用 `notify_submit(on_submit)` 持久化提交水位；
- `hybrid` 模式仅在尚未跨越最终 `configure` / `Share` 边界的明确失败时允许回退浏览器，绝不二次提交。
"""
from __future__ import annotations

import asyncio
import inspect
import io
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ...browser.human_interaction import DEFAULT_HUMAN_ENGINE, human_wait
from ...browser.identity import Identity
from ...browser.manager import BrowserManager
from ...media_tools import MediaToolError, video_cover_and_meta
from .._submit import notify_stage as _notify_stage, notify_submit as _notify_submit
from .client import InstagramClient
from .extract import has_ig_login_cookies
from .protocol import ig_thumbnail_wait_ms
from .resolve import media_id_to_shortcode

INSTAGRAM_HOME_URL = "https://www.instagram.com/"
VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".webm"})
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def build_ig_caption(title: str, desc: str, topics: str = "", limit: int = 2200) -> str:
    """合并标题、正文与去重 #话题，遵守 Instagram 2200 字符上限。"""
    parts: List[str] = []
    t = (title or "").strip()
    d = (desc or "").strip()
    if t:
        parts.append(t)
    if d and d != t:
        parts.append(d)
    tags: List[str] = []
    seen: set[str] = set()
    for raw in (topics or "").replace("，", ",").split(","):
        tag = raw.strip().lstrip("#").strip()
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            tags.append(f"#{tag}")
    if tags:
        parts.append(" ".join(tags))
    return "\n".join(parts).strip()[:limit]


def _prepare_image_jpeg(path: Path) -> Tuple[bytes, int, int]:
    """读取本地图片并统一转为 RGB JPEG 字节流，返回 `(jpeg_bytes, width, height)`。"""
    from PIL import Image, ImageOps

    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB",):
            im = im.convert("RGB")
        w, h = im.size
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=92)
        return buf.getvalue(), max(1, int(w)), max(1, int(h))


def _pad_jpeg_to_aspect(raw_jpeg: bytes, target_ratio: float = 9.0 / 16.0) -> Tuple[bytes, int, int]:
    """当服务端提示封面比例不符时，将封面居中黑边补齐到目标比例后重传。"""
    from PIL import Image

    with Image.open(io.BytesIO(raw_jpeg)) as im:
        im = im.convert("RGB")
        w, h = max(1, im.width), max(1, im.height)
        cur = w / float(h)
        if abs(cur - target_ratio) < 0.02:
            nw, nh = 1080, int(round(1080 / target_ratio))
            resized = im.resize((nw, nh))
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=90)
            return buf.getvalue(), nw, nh
        if cur > target_ratio:
            nw = max(640, min(1080, w))
            nh = max(640, int(round(nw / target_ratio)))
            scaled_h = max(1, int(round(nw / cur)))
            canvas = Image.new("RGB", (nw, nh), (0, 0, 0))
            canvas.paste(im.resize((nw, scaled_h)), (0, (nh - scaled_h) // 2))
        else:
            nh = max(640, min(1920, h))
            nw = max(640, int(round(nh * target_ratio)))
            scaled_w = max(1, int(round(nh * cur)))
            canvas = Image.new("RGB", (nw, nh), (0, 0, 0))
            canvas.paste(im.resize((scaled_w, nh)), ((nw - scaled_w) // 2, 0))
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=90)
        return buf.getvalue(), nw, nh


def _fallback_jpeg_cover(width: int = 720, height: int = 1280) -> bytes:
    from PIL import Image

    im = Image.new("RGB", (max(64, int(width)), max(64, int(height))), (18, 18, 18))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _prepare_video_and_cover(path: Path) -> Tuple[bytes, bytes, float, int, int]:
    """读取视频字节、提取首帧 JPEG 封面及 `(duration_sec, width, height)`。"""
    video_bytes = path.read_bytes()
    try:
        cover_bytes, meta = video_cover_and_meta(path)
        vmeta = meta.get("video") or {}
        width = int(vmeta.get("width") or 720)
        height = int(vmeta.get("height") or 1280)
        duration_sec = max(0.5, float(vmeta.get("duration") or 5000) / 1000.0)
        return video_bytes, cover_bytes, duration_sec, width, height
    except MediaToolError:
        return video_bytes, _fallback_jpeg_cover(720, 1280), 5.0, 720, 1280


def _validate_files(media_type: str, files: Iterable[str]) -> Tuple[List[Path], str]:
    resolved: List[Path] = []
    for f in files or []:
        if not f:
            continue
        p = Path(f).expanduser()
        if p.is_file():
            resolved.append(p.resolve())
    if not resolved:
        return [], "没有可用的本地媒体文件（路径不存在）"
    if len(resolved) > 10:
        return [], "Instagram 单次发布最多支持 10 个媒体文件"
    for p in resolved:
        ext = p.suffix.lower()
        if ext not in VIDEO_EXTS and ext not in IMAGE_EXTS:
            return [], f"不支持的媒体文件格式: {p.name}"
    if media_type == "video" and len(resolved) == 1 and resolved[0].suffix.lower() not in VIDEO_EXTS:
        return [], f"视频发布需要视频文件(.mp4/.mov): {resolved[0].name}"
    return resolved, ""


async def publish_instagram_api(
    client: InstagramClient,
    media_type: str,
    title: str,
    desc: str,
    files: Sequence[str],
    *,
    topics: str = "",
    allow_comment: bool = True,
    ig_options: Optional[Mapping[str, Any]] = None,
    on_submit: Optional[Callable[[], Any]] = None,
    on_stage: Optional[Callable[[str], Any]] = None,
    thumbnail_wait_cap_seconds: float = 7.5,
) -> Tuple[bool, str, str]:
    """通过 Instagram 移动端接口执行两阶段/三阶段媒体上传与 configure 提交。"""
    paths, v_err = _validate_files(media_type, files)
    if v_err:
        return False, "", v_err
    if not client.session.is_logged_in:
        return False, "", "logged_out:账号缺少有效的 Instagram sessionid，请重新登录"

    opts = dict(ig_options or {})
    post_kind = str(opts.get("post_kind") or "auto").strip().lower()
    share_to_feed = bool(opts.get("share_to_feed", True))
    caption = build_ig_caption(title, desc, topics)
    disable_comments = not bool(allow_comment)

    async with client.session_scope():
        # 多文件 -> 轮播图集 (carousel / sidecar)
        if len(paths) > 1:
            await _notify_stage(on_stage, f"正在上传 Instagram 轮播素材 (共 {len(paths)} 项)…")
            children: List[Dict[str, Any]] = []
            for idx, p in enumerate(paths, 1):
                await _notify_stage(on_stage, f"正在上传第 {idx}/{len(paths)} 项媒体…")
                if p.suffix.lower() in VIDEO_EXTS:
                    v_bytes, c_bytes, dur, w, h = _prepare_video_and_cover(p)
                    up_id, err = await client.rupload_video_bytes(
                        v_bytes, duration_sec=dur, width=w, height=h, is_sidecar=True
                    )
                    if not up_id:
                        return False, "", f"第 {idx} 项视频上传失败: {err}"
                    _, c_err = await client.rupload_photo_bytes(
                        c_bytes, upload_id=up_id, width=w, height=h, is_sidecar=True, media_type="2"
                    )
                    if c_err:
                        return False, "", f"第 {idx} 项视频封面上传失败: {c_err}"
                    wait_s = min(float(thumbnail_wait_cap_seconds), ig_thumbnail_wait_ms(len(v_bytes)) / 1000.0)
                    if wait_s > 0:
                        await asyncio.sleep(wait_s)
                    children.append({
                        "kind": "video",
                        "upload_id": up_id,
                        "width": w,
                        "height": h,
                        "duration_sec": dur,
                    })
                else:
                    img_bytes, w, h = _prepare_image_jpeg(p)
                    up_id, err = await client.rupload_photo_bytes(
                        img_bytes, width=w, height=h, is_sidecar=True, media_type="1"
                    )
                    if not up_id:
                        return False, "", f"第 {idx} 项图片上传失败: {err}"
                    children.append({
                        "kind": "image",
                        "upload_id": up_id,
                        "width": w,
                        "height": h,
                    })
            await _notify_stage(on_stage, "正在提交 Instagram 轮播 configure_sidecar…")
            return await client.configure_sidecar(
                children,
                caption=caption,
                disable_comments=disable_comments,
                on_submit=on_submit,
            )

        single = paths[0]
        is_story = post_kind == "story"
        if single.suffix.lower() in VIDEO_EXTS:
            is_reel = (post_kind in {"auto", "reel"}) and not is_story
            await _notify_stage(on_stage, "正在准备视频与提取封面…")
            v_bytes, c_bytes, dur, w, h = _prepare_video_and_cover(single)
            shared_uid = str(int(time.time() * 1000))
            shared_wf = f"{shared_uid}_{uuid.uuid4()}_{'Mixed' if is_reel else 'Video'}_0"
            await _notify_stage(on_stage, "正在上传 Instagram 视频流 (rupload_igvideo)…")
            up_id, err = await client.rupload_video_bytes(
                v_bytes,
                upload_id=shared_uid,
                duration_sec=dur,
                width=w,
                height=h,
                is_reel=is_reel,
                is_story=is_story,
                waterfall_id=shared_wf,
            )
            if not up_id:
                return False, "", err or "视频字节上传失败"
            await _notify_stage(on_stage, "正在上传同 ID 视频封面 (rupload_igphoto)…")
            _, c_err = await client.rupload_photo_bytes(
                c_bytes,
                upload_id=up_id,
                width=w,
                height=h,
                is_reel=is_reel,
                media_type="2",
                waterfall_id=shared_wf,
            )
            if c_err and any(k in c_err.lower() for k in ("aspect ratio", "right format")):
                target_ar = (9.0 / 16.0) if (is_reel or is_story) else max(0.8, min(1.91, w / float(max(1, h))))
                padded_bytes, pw, ph = _pad_jpeg_to_aspect(c_bytes, target_ar)
                _, c_err = await client.rupload_photo_bytes(
                    padded_bytes,
                    upload_id=up_id,
                    width=pw,
                    height=ph,
                    is_reel=is_reel,
                    media_type="2",
                    waterfall_id=shared_wf,
                )
            if c_err:
                return False, "", f"视频封面上传失败: {c_err}"
            # 封面上传完成后等待服务端转码缓冲再执行 configure
            wait_s = min(float(thumbnail_wait_cap_seconds), ig_thumbnail_wait_ms(len(v_bytes)) / 1000.0)
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            await _notify_stage(on_stage, "正在提交 Instagram 视频 configure…")
            return await client.configure_video(
                up_id,
                duration_sec=dur,
                width=w,
                height=h,
                caption=caption,
                is_reel=is_reel,
                is_story=is_story,
                disable_comments=disable_comments,
                share_to_feed=share_to_feed,
                on_submit=on_submit,
            )

        # 单图发布 (rupload_igphoto -> configure / configure_to_story)
        await _notify_stage(on_stage, "正在上传 Instagram 图片 (rupload_igphoto)…")
        img_bytes, w, h = _prepare_image_jpeg(single)
        up_id, err = await client.rupload_photo_bytes(img_bytes, width=w, height=h, media_type="1")
        if not up_id:
            return False, "", err or "图片上传失败"
        await _notify_stage(on_stage, "正在提交 Instagram 图片 configure…")
        return await client.configure_photo(
            up_id,
            width=w,
            height=h,
            caption=caption,
            disable_comments=disable_comments,
            is_story=is_story,
            on_submit=on_submit,
        )


async def publish_instagram_browser(
    mgr: BrowserManager,
    identity: Optional[Identity],
    storage_state: str,
    media_type: str,
    title: str,
    desc: str,
    files: Sequence[str],
    *,
    topics: str = "",
    allow_comment: bool = True,
    headed: bool = True,
    timeout_seconds: int = 180,
    on_submit: Optional[Callable[[], Any]] = None,
    on_stage: Optional[Callable[[str], Any]] = None,
) -> Tuple[bool, str, str]:
    """在账号隔离浏览器上下文内通过 Instagram Web Create 弹窗完成图文/轮播/Reels 发布。"""
    if identity is None:
        return False, "", "缺少账号浏览器隔离身份配置"
    paths, v_err = _validate_files(media_type, files)
    if v_err:
        return False, "", v_err
    if not has_ig_login_cookies(storage_state):
        return False, "", "logged_out:Instagram 登录态缺失或已过期，请先登录"

    caption = build_ig_caption(title, desc, topics)
    await _notify_stage(on_stage, "正在打开 Instagram 创作页面…")

    ctx_mgr = mgr.temporary_headed_context(identity) if headed else None
    page = None
    submitted = False
    captured_url = ""
    configure_err = ""

    async def _on_response(resp) -> None:
        nonlocal captured_url, configure_err
        u = str(resp.url or "")
        if "/api/v1/media/configure" not in u and "/graphql" not in u:
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        if not isinstance(payload, Mapping):
            return
        if "/api/v1/media/configure" in u:
            if str(payload.get("status") or "").lower() == "fail":
                configure_err = str(payload.get("message") or "configure_failed")
                return
            media = payload.get("media") if isinstance(payload.get("media"), Mapping) else {}
            code = str(media.get("code") or "")
            pk = str(media.get("pk") or media.get("id") or "")
            if not code and pk:
                code = media_id_to_shortcode(pk)
            if code:
                kind = "reel" if "configure_to_clips" in u else "p"
                captured_url = f"https://www.instagram.com/{kind}/{code}/"
            return
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        for v in data.values():
            if isinstance(v, Mapping):
                media = v.get("media") if isinstance(v.get("media"), Mapping) else v
                code = str(media.get("code") or media.get("shortcode") or "")
                if code and len(code) >= 6:
                    kind = "reel" if media_type == "video" and len(paths) == 1 else "p"
                    captured_url = f"https://www.instagram.com/{kind}/{code}/"
                    return

    async def _click_create_entry(active_page) -> bool:
        svg_selectors = (
            'svg[aria-label*="New post" i]',
            'svg[aria-label*="Create" i]',
            'svg[aria-label*="新帖子"]',
            'svg[aria-label*="创建"]',
            'svg[aria-label*="建立"]',
            'svg[aria-label*="新貼文"]',
            'svg[aria-label*="Plus icon" i]',
        )
        for sel in svg_selectors:
            try:
                svgs = active_page.locator(sel)
                for idx in range(min(await svgs.count(), 8)):
                    svg = svgs.nth(idx)
                    if not await svg.is_visible():
                        continue
                    anc = svg.locator(
                        "xpath=ancestor::*[self::a or self::button or @role='button' or @role='link'][1]"
                    )
                    target = anc if await anc.count() else svg
                    if await target.is_visible():
                        await DEFAULT_HUMAN_ENGINE.click_locator(
                            target, page=active_page, timeout=3000
                        )
                        return True
            except Exception:
                continue

        exact_texts = {"Create", "创建", "新帖子", "建立", "新貼文"}
        try:
            links = active_page.locator('a, [role="link"], [role="button"], button')
            for idx in range(min(await links.count(), 30)):
                cand = links.nth(idx)
                if not await cand.is_visible():
                    continue
                txt = ((await cand.inner_text()) or "").strip()
                if txt in exact_texts:
                    await DEFAULT_HUMAN_ENGINE.click_locator(
                        cand, page=active_page, timeout=3000
                    )
                    return True
        except Exception:
            pass
        return False

    async def _click_post_submenu_if_present(active_page) -> bool:
        exact_post = {"post", "帖子", "貼文", "发布"}
        try:
            nodes = active_page.locator('a, [role="link"], [role="menuitem"], [role="button"]')
            for idx in range(min(await nodes.count(), 30)):
                cand = nodes.nth(idx)
                if not await cand.is_visible():
                    continue
                txt = ((await cand.inner_text()) or "").strip().lower()
                if txt in exact_post:
                    await DEFAULT_HUMAN_ENGINE.click_locator(
                        cand, page=active_page, timeout=3000
                    )
                    return True
        except Exception:
            pass
        return False

    async def _drive(active_page) -> Tuple[bool, str, str]:
        nonlocal submitted
        active_page.on("response", _on_response)
        try:
            await active_page.goto(INSTAGRAM_HOME_URL, wait_until="domcontentloaded", timeout=45000)

            # 轮询等待 Instagram React SPA 水合完成，同时自动跳过 onetap / 通知 / Cookie 弹窗
            clicked_create = False
            nav_deadline = asyncio.get_running_loop().time() + 25.0
            while asyncio.get_running_loop().time() < nav_deadline:
                cur_url = str(active_page.url or "").lower()
                if "/accounts/login" in cur_url:
                    return False, "", "logged_out:Instagram 浏览器会话未登录，请重新登录"
                if "/challenge/" in cur_url or "/checkpoint/" in cur_url:
                    return False, "", "verification_required:Instagram 账号需要完成安全验证"

                for dismiss_txt in ("Not Now", "Not now", "以后再说", "稍后", "暂不", "Decline optional cookies"):
                    btn = active_page.locator(
                        f'button:has-text("{dismiss_txt}"), div[role="button"]:has-text("{dismiss_txt}")'
                    ).first
                    try:
                        if await btn.count() and await btn.is_visible():
                            await DEFAULT_HUMAN_ENGINE.click_locator(
                                btn, page=active_page, timeout=1500
                            )
                            await human_wait(active_page, 600)
                    except Exception:
                        pass

                clicked_create = await _click_create_entry(active_page)
                if clicked_create:
                    break
                await human_wait(active_page, 800)

            if not clicked_create:
                return False, "", "未找到 Instagram 网页端「创建(Create)」入口按钮"

            await human_wait(active_page, 1200)
            # 仅当创作弹窗尚未打开（即点击 Create 弹出的是 Post/Live 二级菜单）时，才点击精确匹配的 Post/帖子 菜单项
            dialog_ready = bool(
                await active_page.locator('div[role="dialog"] input[type="file"], div[role="dialog"]').count()
            )
            if not dialog_ready:
                if await _click_post_submenu_if_present(active_page):
                    await human_wait(active_page, 1200)

            await _notify_stage(on_stage, "正在注入本地媒体文件…")
            dialog_file = active_page.locator('div[role="dialog"] input[type="file"]').first
            any_file = active_page.locator('input[type="file"]').first
            if await dialog_file.count():
                await dialog_file.set_input_files([str(p) for p in paths])
            elif await any_file.count():
                await any_file.set_input_files([str(p) for p in paths])
            else:
                pick_btn = active_page.locator(
                    'div[role="dialog"] button:has-text("Select from computer"), '
                    'div[role="dialog"] button:has-text("从电脑中选择"), '
                    'div[role="dialog"] button:has-text("从电脑选择"), '
                    'div[role="dialog"] div[role="button"]:has-text("Select from computer"), '
                    'div[role="dialog"] div[role="button"]:has-text("从电脑中选择")'
                ).first
                async with active_page.expect_file_chooser(timeout=15000) as fc_info:
                    await DEFAULT_HUMAN_ENGINE.click_locator(
                        pick_btn, page=active_page, timeout=5000
                    )
                chooser = await fc_info.value
                await chooser.set_files([str(p) for p in paths])
            await human_wait(active_page, 2500)

            share_sel = (
                'div[role="dialog"] div[role="button"]:text-is("Share"), '
                'div[role="dialog"] button:text-is("Share"), '
                'div[role="dialog"] div[role="button"]:text-is("分享"), '
                'div[role="dialog"] button:text-is("分享")'
            )
            next_sel = (
                'div[role="dialog"] div[role="button"]:text-is("Next"), '
                'div[role="dialog"] button:text-is("Next"), '
                'div[role="dialog"] div[role="button"]:text-is("下一步"), '
                'div[role="dialog"] button:text-is("下一步")'
            )

            # 状态驱动推进裁切 / 滤镜 / 封面步骤，直至顶栏出现精确的 Share/分享 按钮
            for _step in range(4):
                for ok_txt in ("OK", "好", "确定", "知道了", "Got it"):
                    ok_btn = active_page.locator(f'div[role="dialog"] button:text-is("{ok_txt}")').first
                    try:
                        if await ok_btn.count() and await ok_btn.is_visible():
                            await DEFAULT_HUMAN_ENGINE.click_locator(
                                ok_btn, page=active_page, timeout=1500
                            )
                            await human_wait(active_page, 600)
                    except Exception:
                        pass
                share_check = active_page.locator(share_sel).first
                if await share_check.count() and await share_check.is_visible():
                    break
                await _notify_stage(on_stage, "正在推进裁切与滤镜步骤…")
                next_btn = active_page.locator(next_sel).first
                await next_btn.wait_for(state="visible", timeout=20000)
                await DEFAULT_HUMAN_ENGINE.click_locator(
                    next_btn, page=active_page, timeout=5000
                )
                await human_wait(active_page, 1500)

            if caption:
                await _notify_stage(on_stage, "正在填写帖子文案与话题…")
                cap_box = active_page.locator(
                    'div[role="dialog"] div[aria-label*="Write a caption" i], '
                    'div[role="dialog"] div[aria-label*="输入说明文字"], '
                    'div[role="dialog"] div[aria-label*="撰写说明"], '
                    'div[role="dialog"] div[contenteditable="true"][role="textbox"], '
                    'div[role="dialog"] textarea'
                ).first
                await cap_box.wait_for(state="visible", timeout=10000)
                await DEFAULT_HUMAN_ENGINE.type_text(
                    cap_box, caption, page=active_page, clear=False, timeout=5000
                )
                await human_wait(active_page, 500)

            if not allow_comment:
                adv = active_page.locator(
                    'div[role="dialog"] span:has-text("Advanced settings"), '
                    'div[role="dialog"] span:has-text("高级设置")'
                ).first
                try:
                    if await adv.count() and await adv.is_visible():
                        await DEFAULT_HUMAN_ENGINE.click_locator(
                            adv, page=active_page, timeout=2500
                        )
                        await human_wait(active_page, 500)
                        toggle = active_page.locator('div[role="dialog"] input[type="checkbox"]').first
                        if await toggle.count() and not await toggle.is_checked():
                            await DEFAULT_HUMAN_ENGINE.click_locator(
                                toggle, page=active_page, timeout=2500
                            )
                except Exception:
                    pass

            share_btn = active_page.locator(share_sel).first
            await share_btn.wait_for(state="visible", timeout=12000)

            await _notify_stage(on_stage, "正在点击 Share 提交发布…")
            await _notify_submit(on_submit)
            submitted = True
            await DEFAULT_HUMAN_ENGINE.click_locator(
                share_btn, page=active_page, timeout=5000
            )

            deadline = asyncio.get_running_loop().time() + max(30, int(timeout_seconds))
            success_markers = (
                "Your post has been shared",
                "Your reel has been shared",
                "Post shared",
                "Reel shared",
                "已分享你的帖子",
                "已分享你的快拍",
                "已分享你的 Reel",
                "已分享你的视频",
                "你的帖子已分享",
                "你的快拍已分享",
                "你的 Reel 已分享",
                "帖子已分享",
                "Reel 已分享",
                "已發佈你的貼文",
                "已分享你的連續短片",
            )
            while asyncio.get_running_loop().time() < deadline:
                if captured_url:
                    return True, captured_url, ""
                if configure_err:
                    return False, "", f"Instagram 发布接口拒绝: {configure_err}"
                body_txt = await active_page.evaluate(
                    "() => (document.querySelector('div[role=\"dialog\"]')?.innerText || '')"
                )
                if any(m.lower() in str(body_txt).lower() for m in success_markers):
                    return True, captured_url or INSTAGRAM_HOME_URL, ""
                await human_wait(active_page, 1500)

            return False, "", "write_uncertain:已点击 Share 提交，但等待 Instagram 发布完成确认超时，请前往主页核对"
        finally:
            try:
                active_page.remove_listener("response", _on_response)
            except Exception:
                pass

    try:
        if ctx_mgr is not None:
            async with ctx_mgr as ctx:
                page = await ctx.new_page()
                return await _drive(page)
        page = await mgr.new_page(identity, block_media=False)
        try:
            return await _drive(page)
        finally:
            if page is not None and not page.is_closed():
                await page.close()
    except Exception as exc:
        if submitted:
            return False, "", f"write_uncertain:Instagram 浏览器发布提交后连接中断({exc})"
        return False, "", f"browser_publish_error:{type(exc).__name__}:{exc}"


async def publish_instagram(
    mgr: Optional[BrowserManager],
    identity: Optional[Identity],
    storage_state: str,
    media_type: str,
    title: str,
    desc: str,
    files: Sequence[str],
    *,
    topics: str = "",
    visibility: str = "public",
    allow_comment: bool = True,
    ig_options: Optional[Mapping[str, Any]] = None,
    mode: str = "hybrid",
    headed: bool = True,
    timeout_seconds: int = 180,
    on_submit: Optional[Callable[[], Any]] = None,
    on_stage: Optional[Callable[[str], Any]] = None,
    api_client: Optional[InstagramClient] = None,
) -> Tuple[bool, str, str]:
    """Instagram 统一发布入口（`hybrid` / `api` / `browser`）。"""
    _ = visibility  # Instagram 帖子公开/私密由账号自身隐私属性决定
    norm_mode = str(mode or "hybrid").strip().lower()
    if norm_mode not in {"hybrid", "api", "browser"}:
        norm_mode = "hybrid"

    submitted = False

    async def _invoke_submit(ch: str) -> None:
        nonlocal submitted
        submitted = True
        if on_submit is None:
            return
        try:
            params = inspect.signature(on_submit).parameters
            accepts_channel = any(
                p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
                for p in params.values()
            )
        except Exception:
            accepts_channel = False
        res = on_submit(ch) if accepts_channel else on_submit()  # type: ignore[misc]
        if inspect.isawaitable(res):
            await res

    async def _tracked_api_submit() -> None:
        await _invoke_submit("api")

    async def _tracked_browser_submit() -> None:
        await _invoke_submit("browser")

    opts = dict(ig_options or {})
    post_kind = str(opts.get("post_kind") or "auto").strip().lower()
    # Story 快拍桌面网页端无原生发布入口，固定走移动端 Story 接口
    if norm_mode in {"api", "hybrid"} or post_kind == "story":
        cli = api_client or InstagramClient(
            storage_state,
            getattr(identity, "ua", "") if identity else "",
            proxy=(getattr(identity, "proxy", "") if identity else ""),
            account_seed=(getattr(identity, "fp_seed", "") or getattr(identity, "profile_dir", "")) if identity else "",
            timezone_id=(getattr(identity, "timezone_id", "Asia/Shanghai") if identity else "Asia/Shanghai"),
        )
        if api_client is None:
            async with cli.session_scope():
                ok, url, err = await publish_instagram_api(
                    cli,
                    media_type,
                    title,
                    desc,
                    files,
                    topics=topics,
                    allow_comment=allow_comment,
                    ig_options=opts,
                    on_submit=_tracked_api_submit,
                    on_stage=on_stage,
                )
        else:
            ok, url, err = await publish_instagram_api(
                cli,
                media_type,
                title,
                desc,
                files,
                topics=topics,
                allow_comment=allow_comment,
                ig_options=opts,
                on_submit=_tracked_api_submit,
                on_stage=on_stage,
            )
        if ok:
            first_comment = str(opts.get("first_comment") or "").strip()
            if first_comment and url and post_kind != "story":
                try:
                    async with cli.session_scope():
                        await cli.post_comment(url, first_comment)
                except Exception:
                    pass
            return True, url, ""
        if (
            norm_mode == "api"
            or post_kind == "story"
            or submitted
            or str(err or "").startswith(("write_uncertain:", "logged_out:", "verification_required:"))
            or mgr is None
            or identity is None
        ):
            return False, "", err

    return await publish_instagram_browser(
        mgr,  # type: ignore[arg-type]
        identity,
        storage_state,
        media_type,
        title,
        desc,
        files,
        topics=topics,
        allow_comment=allow_comment,
        headed=headed,
        timeout_seconds=timeout_seconds,
        on_submit=_tracked_browser_submit,
        on_stage=on_stage,
    )
