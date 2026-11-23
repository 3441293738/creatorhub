"""X (Twitter) 网页端原生发布器（支持纯文本、1~4 张高清图集、单视频/GIF、回复权限与首评串推）。

安全不变量：
- 仅在实际触发最终 `Post` (`tweetButton`) 提交动作或监测到 `CreateTweet` 请求发出时才标记 `submitted` 并调用 `notify_submit`；
- 自动收起 `#话题` / `@提及` 联想下拉遮罩 (`typeaheadDropdown` / `#layers` 透明背景层)，通过 `HumanInteractionEngine` 执行真实硬件轨迹点击（确保单次可信点击不重复提交）；
- 优先拦截 `CreateTweet` / `CreateNoteTweet` GraphQL 响应提取精确 `rest_id` 避免主页首帖竞态；
- 绝不自动点击任何验证码/挑战框，遇到 `/account/access` 立即返回 `verification_required:` 交由风控中心托管。
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ...browser.human_interaction import DEFAULT_HUMAN_ENGINE, human_wait
from ...browser.identity import Identity
from ...browser.manager import BrowserManager
from .._submit import notify_stage as _notify_stage
from .client import classify_twitter_payload_error
from .extract import extract_graphql_tweets, parse_twitter_aweme
from .formatter import (
    DEFAULT_TWEET_LIMIT,
    PREMIUM_TWEET_LIMIT,
    build_tweet_caption,
    trim_to_twitter_limit,
    twitter_weighted_length,
)
from .protocol import has_twitter_login_cookies
from .selectors import (
    ACCOUNT_LOCKED_URL_PARTS,
    ACCOUNT_SUSPENDED_URL_PARTS,
    COMPOSE_FILE_INPUT_SELECTORS,
    COMPOSE_SUBMIT_BUTTON_SELECTORS,
    COMPOSE_TEXTBOX_SELECTORS,
    REPLY_AUDIENCE_TRIGGER_SELECTORS,
)
from .urls import COMPOSE_URL, GQL_CREATE_TWEET_HINTS, HOME_URL, tweet_url

VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".webm"})
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})

_REPLY_PERMISSION_LABELS: Dict[str, Tuple[str, ...]] = {
    "everyone": ("Everyone", "所有人", "任何人"),
    "following": ("Accounts you follow", "People you follow", "你关注的账号", "你关注的人"),
    "verified": ("Verified accounts", "认证账号", "已认证账号"),
    "mentioned": ("Only accounts you mention", "Only people you mention", "仅你提及的账号", "只有你提及的人"),
}

_TRAILING_ENTITY_RE = re.compile(r"(?:^|\s)[#@][^\s#@]+$")


def validate_twitter_publish_files(media_type: str, files: Sequence[str]) -> Tuple[List[Path], str]:
    """校验 Twitter 发布本地文件：允许 0 文件（纯文本）、1~4 张图片或 1 个视频。"""
    paths: List[Path] = []
    for raw in files or []:
        if not raw:
            continue
        p = Path(raw)
        if not p.is_file():
            return [], f"本地媒体文件不存在: {raw}"
        if p.stat().st_size <= 0:
            return [], f"本地媒体文件为空: {p.name}"
        paths.append(p)

    if not paths:
        return [], ""

    mtype = (media_type or "").strip().lower()
    has_video = any(p.suffix.lower() in VIDEO_EXTS for p in paths)
    if mtype == "video" or has_video:
        if len(paths) > 1:
            return [], "X (Twitter) 视频发布单次仅支持 1 个视频文件"
        if paths[0].suffix.lower() not in VIDEO_EXTS and paths[0].suffix.lower() != ".gif":
            return [], f"不支持的视频格式: {paths[0].suffix}"
        return paths, ""

    if len(paths) > 4:
        return [], "X (Twitter) 图文发布单次最多支持 4 张图片"
    for p in paths:
        if p.suffix.lower() not in IMAGE_EXTS:
            return [], f"不支持的图片格式: {p.suffix}"
    return paths, ""


async def _find_first_visible(page, selectors: Sequence[str], timeout_ms: int = 8000):
    deadline = asyncio.get_running_loop().time() + max(0.5, timeout_ms / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    return loc
            except Exception:
                continue
        await human_wait(page, 350)
    return None


async def _dismiss_compose_overlays(page) -> None:
    """安全关闭会遮挡 `tweetButton` 的 `#话题`/`@提及` 联想浮层与非破坏性首次发帖提示弹窗。

    注意：绝不在没有联想框时盲按 `Escape`，以免触发 `/compose/post` 弹窗自身的「放弃草稿」确认框。
    """
    try:
        # 1) 若存在 #hashtag / @mention 联想下拉列表，按一次 Escape 关闭联想框及其全屏透明遮罩
        typeahead = page.locator(
            '[data-testid="typeaheadDropdown"], div[role="listbox"][id*="typeahead"]'
        ).first
        if await typeahead.count() and await typeahead.is_visible():
            await page.keyboard.press("Escape")
            await human_wait(page, 300)
    except Exception:
        pass

    try:
        # 2) 若存在首次发帖引导提示框（如 Unlock more on X / Automated labels / Got it），使用拟人轨迹点击知道了/关闭按钮（排除放弃草稿按钮）
        dialog = page.locator('[data-testid="sheetDialog"]').first
        if await dialog.count() and await dialog.is_visible():
            safe_words = (
                "got it",
                "ok",
                "close",
                "maybe later",
                "not now",
                "知道了",
                "好的",
                "关闭",
                "稍后",
                "暂不",
            )
            danger_words = ("discard", "delete", "cancel", "放弃", "删除")
            btns = dialog.locator('button, [role="button"]')
            for idx in range(min(await btns.count(), 12)):
                cand = btns.nth(idx)
                if not await cand.is_visible():
                    continue
                tid = str(await cand.get_attribute("data-testid") or "").lower()
                if "confirmationsheetcancel" in tid or "discard" in tid:
                    continue
                txt = str(
                    (await cand.inner_text()) or (await cand.get_attribute("aria-label")) or ""
                ).strip().lower()
                if not txt or any(w in txt for w in danger_words):
                    continue
                if any(txt == w or w in txt for w in safe_words):
                    await DEFAULT_HUMAN_ENGINE.click_locator(cand, page=page, timeout=2500)
                    await human_wait(page, 400)
                    break
    except Exception:
        pass


async def _wait_for_media_upload_ready(page, *, has_video: bool, timeout_ms: int) -> None:
    """等待附件预览区的上传进度条消失，确保视频/图片已在 X 服务端完成上传与处理。"""
    deadline = asyncio.get_running_loop().time() + max(3.0, timeout_ms / 1000.0)
    # 先给前端渲染附件预览留出短暂时间
    await human_wait(page, 1200 if has_video else 600)
    while asyncio.get_running_loop().time() < deadline:
        try:
            uploading = await page.evaluate(
                """() => {
                    const att = document.querySelector('[data-testid="attachments"]');
                    if (!att) return false;
                    const bar = att.querySelector('[role="progressbar"]');
                    if (bar) {
                        const val = bar.getAttribute('aria-valuenow');
                        if (val === null || Number(val) < 100) return true;
                    }
                    return false;
                }"""
            )
            if not uploading:
                return
        except Exception:
            return
        await human_wait(page, 800)


async def _click_tweet_submit_once(
    page,
    submit_btn,
    *,
    mark_submitted: Callable[[], Any],
    was_request_sent: Callable[[], bool],
) -> None:
    """单次安全点击发推/回复按钮：

    - 先收起可能遮挡按钮的 `#layers` 联想浮层；
    - 校验按钮处于可用状态后，通过 `DEFAULT_HUMAN_ENGINE.click_locator` 派发真实硬件鼠标轨迹与按压点击（严禁合成 JS `el.click()`）；
    - 仅在点击动作实际送达或请求已发出时标记 `submitted`，避免预检失败误锁为 `write_uncertain`。
    """
    await _dismiss_compose_overlays(page)
    enabled = await submit_btn.evaluate(
        """el => !el.disabled && el.getAttribute('aria-disabled') !== 'true'"""
    )
    if not enabled:
        raise RuntimeError("X 发送按钮仍处于禁用状态（可能字数超限或媒体处理尚未完成）")
    try:
        await DEFAULT_HUMAN_ENGINE.click_locator(submit_btn, page=page, timeout=5000)
        res = mark_submitted()
        if inspect.isawaitable(res):
            await res
        return
    except Exception:
        if was_request_sent():
            res = mark_submitted()
            if inspect.isawaitable(res):
                await res
            return
        raise


async def _apply_reply_permission(page, permission: str) -> None:
    """在发推弹窗内设置回复受众权限（everyone / following / verified / mentioned）。"""
    perm = str(permission or "everyone").strip().lower()
    if perm not in _REPLY_PERMISSION_LABELS or perm == "everyone":
        return
    trigger = await _find_first_visible(page, REPLY_AUDIENCE_TRIGGER_SELECTORS, timeout_ms=3000)
    if trigger is None:
        return
    try:
        await DEFAULT_HUMAN_ENGINE.click_locator(trigger, page=page, timeout=2500)
        await human_wait(page, 500)
        labels = _REPLY_PERMISSION_LABELS[perm]
        for label in labels:
            item = page.locator(f'div[role="menuitem"]:has-text("{label}")').first
            if await item.count() and await item.is_visible():
                await DEFAULT_HUMAN_ENGINE.click_locator(item, page=page, timeout=2000)
                await human_wait(page, 400)
                return
        await page.keyboard.press("Escape")
    except Exception:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass


async def _post_first_comment_reply(
    page,
    tweet_link: str,
    comment_text: str,
    *,
    long_text: bool = False,
) -> None:
    """主推文发布成功后，最佳努力（best-effort）在原帖下方追加首条自评线程。"""
    cap_limit = PREMIUM_TWEET_LIMIT if long_text else DEFAULT_TWEET_LIMIT
    txt = trim_to_twitter_limit(comment_text, limit=cap_limit)
    if not txt or not tweet_link or "/status/" not in tweet_link:
        return
    if _TRAILING_ENTITY_RE.search(txt) and twitter_weighted_length(txt) + 1 <= cap_limit:
        txt = txt + " "
    try:
        await page.goto(tweet_link, wait_until="domcontentloaded", timeout=30000)
        await human_wait(page, 1800)
        reply_box = await _find_first_visible(page, COMPOSE_TEXTBOX_SELECTORS, timeout_ms=8000)
        if reply_box is None:
            reply_btn = page.locator('button[data-testid="reply"]').first
            if await reply_btn.count():
                await DEFAULT_HUMAN_ENGINE.click_locator(reply_btn, page=page, timeout=3000)
                await human_wait(page, 800)
                reply_box = await _find_first_visible(page, COMPOSE_TEXTBOX_SELECTORS, timeout_ms=6000)
        if reply_box is None:
            return
        await DEFAULT_HUMAN_ENGINE.type_text(
            reply_box, txt, page=page, clear=False, timeout=4000
        )
        await human_wait(page, 500)
        await _dismiss_compose_overlays(page)
        send_btn = await _find_first_visible(page, COMPOSE_SUBMIT_BUTTON_SELECTORS, timeout_ms=6000)
        if send_btn is not None:
            await _click_tweet_submit_once(
                page,
                send_btn,
                mark_submitted=lambda: None,
                was_request_sent=lambda: False,
            )
            await human_wait(page, 2000)
    except Exception:
        pass


async def publish_twitter(
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
    tw_options: Optional[Mapping[str, Any]] = None,
    headed: bool = True,
    timeout_seconds: int = 180,
    on_submit: Optional[Callable[[], Any]] = None,
    on_stage: Optional[Callable[[str], Any]] = None,
) -> Tuple[bool, str, str]:
    """在账号专属隔离浏览器中执行 X (Twitter) 推文发布。"""
    _ = visibility
    if mgr is None or identity is None:
        return False, "", "缺少 X (Twitter) 账号浏览器隔离环境"
    if not has_twitter_login_cookies(storage_state):
        return False, "", "logged_out:X (Twitter) 登录态缺失或已过期，请先完成登录或导入 Cookie"

    opts = dict(tw_options or {})
    long_text = bool(opts.get("long_text", False))
    reply_perm = str(opts.get("reply_permission") or "everyone").strip().lower()
    if not allow_comment and reply_perm == "everyone":
        reply_perm = "mentioned"
    first_comment = str(opts.get("first_comment") or "").strip()

    paths, v_err = validate_twitter_publish_files(media_type, files)
    if v_err:
        return False, "", v_err

    cap_limit = PREMIUM_TWEET_LIMIT if long_text else DEFAULT_TWEET_LIMIT
    caption = build_tweet_caption(
        title,
        desc,
        topics,
        long_text=long_text,
        preserve_last_url=True,
    )
    if not paths and not caption:
        return False, "", "X (Twitter) 纯文本发布至少需要填写推文正文或标题"

    submitted = False
    create_req_sent = False
    captured_url = ""
    create_err = ""
    upload_err = ""
    media_finalized = False

    async def _invoke_submit() -> None:
        nonlocal submitted
        if submitted:
            return
        submitted = True
        if on_submit is None:
            return
        try:
            params = inspect.signature(on_submit).parameters
            accepts_ch = any(
                p.kind in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.VAR_POSITIONAL,
                )
                for p in params.values()
            )
        except Exception:
            accepts_ch = False
        res = on_submit("browser") if accepts_ch else on_submit()  # type: ignore[misc]
        if inspect.isawaitable(res):
            await res

    def _on_request(req) -> None:
        nonlocal create_req_sent
        u = str(req.url or "")
        if any(h in u for h in GQL_CREATE_TWEET_HINTS):
            create_req_sent = True

    async def _on_response(resp) -> None:
        nonlocal captured_url, create_err, upload_err, media_finalized
        u = str(resp.url or "")
        if "/media/upload" in u:
            try:
                up_payload = await resp.json()
            except Exception:
                up_payload = None
            if int(resp.status) >= 400 or (isinstance(up_payload, Mapping) and up_payload.get("errors")):
                upload_err = classify_twitter_payload_error(int(resp.status), up_payload)
                return
            if isinstance(up_payload, Mapping):
                pinfo = up_payload.get("processing_info")
                if isinstance(pinfo, Mapping):
                    pstate = str(pinfo.get("state") or "").lower()
                    if pstate == "failed":
                        perr = pinfo.get("error") if isinstance(pinfo.get("error"), Mapping) else {}
                        upload_err = f"X 媒体转码失败: {(perr or {}).get('message') or (perr or {}).get('name') or 'processing_failed'}"
                    elif pstate == "succeeded":
                        media_finalized = True
                elif up_payload.get("media_id_string") and "command=INIT" not in u:
                    media_finalized = True
            return
        if not any(h in u for h in GQL_CREATE_TWEET_HINTS):
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        if not isinstance(payload, Mapping):
            return
        tweets, _ = extract_graphql_tweets(payload)
        if not tweets:
            data = payload.get("data")
            if isinstance(data, Mapping):
                for key in ("create_tweet", "notetweet_create"):
                    sub = data.get(key)
                    if isinstance(sub, Mapping) and isinstance(sub.get("tweet_results"), Mapping):
                        res = sub["tweet_results"].get("result")
                        if isinstance(res, Mapping):
                            tweets = [dict(res)]
                            break
        if tweets:
            aw = parse_twitter_aweme(tweets[0])
            if aw and aw.aweme_id:
                captured_url = tweet_url(aw.aweme_id, aw.author_unique_id)
                return
            rest_id = str(tweets[0].get("rest_id") or "").strip()
            if rest_id:
                captured_url = tweet_url(rest_id)
                return

        if payload.get("errors") or int(resp.status) >= 400:
            create_err = classify_twitter_payload_error(int(resp.status), payload)

    async def _drive(page) -> Tuple[bool, str, str]:
        nonlocal captured_url
        page.on("request", _on_request)
        page.on("response", _on_response)
        try:
            await _notify_stage(on_stage, "正在打开 X 发推编辑器…")
            await page.goto(COMPOSE_URL, wait_until="domcontentloaded", timeout=45000)
            await human_wait(page, 1500)

            cur_url = str(page.url or "").lower()
            if "/login" in cur_url or "/i/flow/login" in cur_url:
                return False, "", "logged_out:X 浏览器会话未登录，请重新登录"
            if any(p in cur_url for p in ACCOUNT_LOCKED_URL_PARTS):
                return False, "", "verification_required:X 账号需要完成安全验证(/account/access)"
            if any(p in cur_url for p in ACCOUNT_SUSPENDED_URL_PARTS):
                return False, "", "verification_required:X 账号已被冻结或停用"

            await _dismiss_compose_overlays(page)
            textbox = await _find_first_visible(page, COMPOSE_TEXTBOX_SELECTORS, timeout_ms=20000)
            if textbox is None:
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=35000)
                await human_wait(page, 1500)
                cur_url = str(page.url or "").lower()
                if "/login" in cur_url or "/i/flow/login" in cur_url:
                    return False, "", "logged_out:X 浏览器会话未登录，请重新登录"
                if any(p in cur_url for p in ACCOUNT_LOCKED_URL_PARTS):
                    return False, "", "verification_required:X 账号需要完成安全验证(/account/access)"
                await _dismiss_compose_overlays(page)
                new_btn = page.locator('[data-testid="SideNav_NewTweet_Button"]').first
                if await new_btn.count() and await new_btn.is_visible():
                    await DEFAULT_HUMAN_ENGINE.click_locator(new_btn, page=page, timeout=3000)
                    await human_wait(page, 1000)
                textbox = await _find_first_visible(page, COMPOSE_TEXTBOX_SELECTORS, timeout_ms=12000)

            if textbox is None:
                return False, "", "未找到 X 网页端发推输入框，可能页面结构变更或受限"

            if caption:
                await _notify_stage(on_stage, "正在输入推文正文与话题…")
                # 末尾如果是 #话题 或 @提及，追加一个空格让编辑器立即闭合实体联想框，防止遮挡 Post 按钮
                text_to_type = caption
                if _TRAILING_ENTITY_RE.search(text_to_type) and twitter_weighted_length(text_to_type) + 1 <= cap_limit:
                    text_to_type = text_to_type + " "
                await DEFAULT_HUMAN_ENGINE.type_text(
                    textbox, text_to_type, page=page, clear=False, timeout=5000
                )
                await human_wait(page, 500)
                await _dismiss_compose_overlays(page)

            if paths:
                await _notify_stage(on_stage, f"正在上传 {len(paths)} 个媒体文件…")
                file_input = None
                for sel in COMPOSE_FILE_INPUT_SELECTORS:
                    loc = page.locator(sel).first
                    if await loc.count():
                        file_input = loc
                        break
                if file_input is None:
                    return False, "", "未找到 X 发推媒体上传控件(input[type=file])"
                await file_input.set_input_files([str(p) for p in paths])
                await human_wait(page, 1500)

            if reply_perm != "everyone":
                await _notify_stage(on_stage, "正在设置推文回复权限…")
                await _apply_reply_permission(page, reply_perm)

            has_video = any(p.suffix.lower() in VIDEO_EXTS for p in paths)
            ready_timeout_ms = 120_000 if has_video else (35_000 if paths else 12_000)
            if paths:
                await _notify_stage(on_stage, "正在等待 X 媒体上传与处理完成…")
                up_deadline = asyncio.get_running_loop().time() + (ready_timeout_ms / 1000.0)
                while asyncio.get_running_loop().time() < up_deadline:
                    if upload_err:
                        return False, "", upload_err
                    dom_up_err = await page.evaluate(
                        """() => {
                            const bodyTxt = document.body ? (document.body.innerText || '') : '';
                            if (bodyTxt.includes('媒体上传失败') || bodyTxt.toLowerCase().includes('failed to upload')) {
                                return 'X 页面提示媒体上传失败';
                            }
                            return '';
                        }"""
                    )
                    if dom_up_err:
                        return False, "", (upload_err or dom_up_err)
                    if media_finalized:
                        break
                    await human_wait(page, 700)
                await _wait_for_media_upload_ready(page, has_video=has_video, timeout_ms=10_000)
                if upload_err:
                    return False, "", upload_err

            await _dismiss_compose_overlays(page)
            submit_btn = await _find_first_visible(
                page, COMPOSE_SUBMIT_BUTTON_SELECTORS, timeout_ms=ready_timeout_ms
            )
            if submit_btn is None:
                return False, "", "X 发送按钮一直处于禁用状态（可能字数超限或视频转码超时）"

            await _notify_stage(on_stage, "正在点击 Post 提交推文…")
            await _click_tweet_submit_once(
                page,
                submit_btn,
                mark_submitted=_invoke_submit,
                was_request_sent=lambda: create_req_sent,
            )

            deadline = asyncio.get_running_loop().time() + max(25, int(timeout_seconds))
            while asyncio.get_running_loop().time() < deadline:
                if captured_url:
                    if first_comment:
                        await _notify_stage(on_stage, "正在追加首条自评线程…")
                        await _post_first_comment_reply(
                            page, captured_url, first_comment, long_text=long_text
                        )
                    return True, captured_url, ""
                if create_err:
                    return False, "", f"X 发布接口拒绝: {create_err}"

                try:
                    toast_href = await page.evaluate(
                        """() => {
                            const toast = document.querySelector('[data-testid="toast"]');
                            if (!toast) return '';
                            const link = toast.querySelector('a[href*="/status/"]');
                            return link ? link.getAttribute('href') || '' : '';
                        }"""
                    )
                    if toast_href and "/status/" in str(toast_href):
                        full = (
                            str(toast_href)
                            if str(toast_href).startswith("http")
                            else f"https://x.com{toast_href}"
                        )
                        captured_url = full
                        if first_comment:
                            await _post_first_comment_reply(
                                page, captured_url, first_comment, long_text=long_text
                            )
                        return True, captured_url, ""
                except Exception:
                    pass

                cur_after = str(page.url or "").lower()
                if any(p in cur_after for p in ACCOUNT_LOCKED_URL_PARTS):
                    return False, "", "verification_required:X 提交后触发安全挑战(/account/access)"

                await human_wait(page, 1000)

            return (
                False,
                "",
                "write_uncertain:已点击 Post 提交，但等待 X 发布确认响应超时，请前往账号主页核对",
            )
        finally:
            try:
                page.remove_listener("request", _on_request)
            except Exception:
                pass
            try:
                page.remove_listener("response", _on_response)
            except Exception:
                pass

    ctx_mgr = mgr.temporary_headed_context(identity) if headed else None
    page = None
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
        if submitted or create_req_sent:
            return False, "", f"write_uncertain:X 浏览器发布提交后连接中断({exc})"
        return False, "", f"browser_publish_error:{type(exc).__name__}:{exc}"
