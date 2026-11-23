"""X (Twitter) 网页端受控互动写操作适配器（评论回复、点赞、转推/引用、关注/取关、发私信）。

安全不变量：
- 所有非幂等写操作在点击提交按钮前一律触发 `notify_submit(on_submit)` 记录提交水位；
- 提交后一旦断连或等待确认超时，统一返回 `write_uncertain:...` 阻止自动重放；
- 绝不自动点击或绕过任何验证码/安全挑战页。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from ..platforms._submit import notify_submit


def classify_twitter_payload_error(
    status_code: int, payload: Any, fallback_text: str = ""
) -> str:
    from ..platforms.twitter.client import classify_twitter_payload_error as _fn

    return _fn(status_code, payload, fallback_text)


from ..platforms.twitter.extract import extract_graphql_tweets
from ..platforms.twitter.formatter import trim_to_twitter_limit
from ..platforms.twitter.resolve import (
    resolve_twitter_tweet_ref,
    resolve_twitter_user_target,
)
from ..platforms.twitter.selectors import (
    COMPOSE_SUBMIT_BUTTON_SELECTORS,
    COMPOSE_TEXTBOX_SELECTORS,
    CONFIRM_SHEET_BUTTON_SELECTORS,
    DM_COMPOSER_INPUT_SELECTORS,
    DM_PIN_PROMPT_SELECTORS,
    DM_PROFILE_BUTTON_SELECTORS,
    DM_SEND_BUTTON_SELECTORS,
    FOLLOW_BUTTON_SELECTORS,
    LIKE_BUTTON_SELECTORS,
    QUOTE_MENUITEM_SELECTORS,
    REPLY_BUTTON_SELECTORS,
    RETWEET_BUTTON_SELECTORS,
    RETWEET_CONFIRM_SELECTORS,
    UNFOLLOW_BUTTON_SELECTORS,
    UNLIKE_BUTTON_SELECTORS,
    UNRETWEET_BUTTON_SELECTORS,
)
from ..platforms.twitter.urls import (
    GQL_CREATE_TWEET_HINTS,
    GQL_FOLLOW_MUTATION_HINTS,
    GQL_LIKE_TWEET_HINTS,
    GQL_RETWEET_HINTS,
    REST_DM_SEND_HINTS,
    dm_conversation_url,
    profile_url,
    tweet_url,
)
from .human_interaction import (
    DEFAULT_HUMAN_ENGINE,
    human_click_first,
    human_fill_first,
    human_scroll,
    human_wait,
)
from .identity import Identity
from .manager import BrowserManager
from .twitter_fetcher import check_twitter_url_risk


async def _wait_visible(page, selectors: Sequence[str], timeout_ms: int = 8000):
    deadline = asyncio.get_running_loop().time() + max(0.5, timeout_ms / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    return loc
            except Exception:
                continue
        await human_wait(page, 300)
    return None


async def _run_in_account_page(
    mgr: BrowserManager,
    identity: Identity,
    *,
    headed: bool,
    fn,
):
    if headed:
        async with mgr.temporary_headed_context(identity) as ctx:
            page = await ctx.new_page()
            try:
                return await fn(page)
            finally:
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception:
                    pass
    page = await mgr.new_page(identity, block_media=False)
    try:
        return await fn(page)
    finally:
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def post_twitter_comment(
    mgr: BrowserManager,
    identity: Identity,
    aweme_id: str,
    content: str,
    *,
    target_url: str = "",
    reply_to_comment_id: str = "",
    headed: bool = True,
    on_submit: Optional[Callable[[], Any]] = None,
) -> Tuple[bool, str, str]:
    """在推文（或指定子评论推文）下发表回复，返回 `(ok, reply_tweet_id_or_ok, error)`。"""
    body = trim_to_twitter_limit(str(content or "").strip(), limit=280)
    if not body:
        return False, "", "评论内容不能为空"

    # 在 X 中每条评论本身也是一条 status 推文；指定 reply_to_comment_id 时直接打开该评论推文即可精准楼中楼回复
    effective_tid = str(reply_to_comment_id or "").strip()
    if effective_tid:
        url = tweet_url(effective_tid)
    else:
        ref = resolve_twitter_tweet_ref(target_url or aweme_id)
        tid = ref.tweet_id if ref else str(aweme_id or "").strip()
        if not tid:
            return False, "", "缺少目标推文 ID 或链接"
        url = target_url or (ref.url if ref else tweet_url(tid))

    submitted = False
    created_id = ""
    api_err = ""

    async def _drive(page) -> Tuple[bool, str, str]:
        nonlocal submitted, created_id, api_err

        async def on_response(resp):
            nonlocal created_id, api_err
            u = str(resp.url or "")
            if not any(h in u for h in GQL_CREATE_TWEET_HINTS):
                return
            try:
                payload = await resp.json()
            except Exception:
                return
            if not isinstance(payload, Mapping):
                return
            tweets, _ = extract_graphql_tweets(payload)
            if tweets:
                rid = str(tweets[0].get("rest_id") or "").strip()
                if rid:
                    created_id = rid
                    return
            if payload.get("errors") or int(resp.status) >= 400:
                api_err = classify_twitter_payload_error(int(resp.status), payload)

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            await human_wait(page, 2000)
            risk_err = check_twitter_url_risk(page.url)
            if risk_err:
                return False, "", risk_err

            box = await _wait_visible(page, COMPOSE_TEXTBOX_SELECTORS, timeout_ms=6000)
            if box is None:
                reply_btn = await _wait_visible(page, REPLY_BUTTON_SELECTORS, timeout_ms=5000)
                if reply_btn is not None:
                    await DEFAULT_HUMAN_ENGINE.click_locator(
                        reply_btn, page=page, timeout=3000
                    )
                    await human_wait(page, 900)
                box = await _wait_visible(page, COMPOSE_TEXTBOX_SELECTORS, timeout_ms=8000)
            if box is None:
                return False, "", "未找到推文回复输入框（该推文可能限制了可回复受众）"

            from ..platforms.twitter.formatter import twitter_weighted_length
            from ..platforms.twitter.publish import (
                _TRAILING_ENTITY_RE,
                _click_tweet_submit_once,
                _dismiss_compose_overlays,
            )

            to_type = (
                body + " "
                if (
                    _TRAILING_ENTITY_RE.search(body)
                    and twitter_weighted_length(body) + 1 <= 280
                )
                else body
            )
            await DEFAULT_HUMAN_ENGINE.type_text(
                box, to_type, page=page, clear=False, timeout=4000
            )
            await human_wait(page, 500)
            await _dismiss_compose_overlays(page)

            send_btn = await _wait_visible(
                page, COMPOSE_SUBMIT_BUTTON_SELECTORS, timeout_ms=8000
            )
            if send_btn is None:
                return False, "", "X 回复按钮不可用（可能受限于作者回复权限设置）"

            async def _mark():
                nonlocal submitted
                if not submitted:
                    submitted = True
                    await notify_submit(on_submit)

            await _click_tweet_submit_once(
                page,
                send_btn,
                mark_submitted=_mark,
                was_request_sent=lambda: bool(created_id or api_err),
            )

            deadline = asyncio.get_running_loop().time() + 22.0
            while asyncio.get_running_loop().time() < deadline:
                if created_id:
                    return True, created_id, ""
                if api_err:
                    return False, "", api_err
                toast_ok = await page.evaluate(
                    """() => {
                        const t = document.querySelector('[data-testid="toast"]');
                        if (!t) return '';
                        const a = t.querySelector('a[href*="/status/"]');
                        if (a) return a.getAttribute('href') || 'ok';
                        const txt = (t.innerText || '').toLowerCase();
                        if (txt.includes('sent') || txt.includes('已发送') || txt.includes('已發送')) return 'ok';
                        return '';
                    }"""
                )
                if toast_ok:
                    ref_new = resolve_twitter_tweet_ref(str(toast_ok))
                    return True, (ref_new.tweet_id if ref_new else "ok"), ""
                await human_wait(page, 800)

            return False, "", "write_uncertain:已点击回复提交，但未收到 X CreateTweet 确认响应，请人工核对"
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

    try:
        return await _run_in_account_page(mgr, identity, headed=headed, fn=_drive)
    except Exception as exc:
        if submitted:
            return False, "", f"write_uncertain:X 评论提交后浏览器连接中断({exc})"
        return False, "", f"browser:{type(exc).__name__}:{exc}"


async def do_twitter_like(
    mgr: BrowserManager,
    identity: Identity,
    tweet_or_url: str,
    *,
    unlike: bool = False,
    headed: bool = True,
    on_submit: Optional[Callable[[], Any]] = None,
) -> Tuple[bool, str]:
    """点赞或取消点赞单条推文（具备当前点赞态幂等预检与响应确认）。"""
    ref = resolve_twitter_tweet_ref(tweet_or_url)
    if ref is None:
        return False, "无效的 X 推文链接或推文 ID"

    submitted = False
    confirmed = False
    api_err = ""

    async def _drive(page) -> Tuple[bool, str]:
        nonlocal submitted, confirmed, api_err

        async def on_response(resp):
            nonlocal confirmed, api_err
            if not submitted:
                return
            u = str(resp.url or "")
            if not any(h in u for h in GQL_LIKE_TWEET_HINTS):
                return
            if int(resp.status) < 400:
                confirmed = True
            else:
                try:
                    payload = await resp.json()
                except Exception:
                    payload = None
                api_err = classify_twitter_payload_error(int(resp.status), payload)

        page.on("response", on_response)
        try:
            await page.goto(ref.url, wait_until="domcontentloaded", timeout=35000)
            await human_wait(page, 2000)
            risk_err = check_twitter_url_risk(page.url)
            if risk_err:
                return False, risk_err

            already_liked = (
                await _wait_visible(page, UNLIKE_BUTTON_SELECTORS, timeout_ms=2500)
            ) is not None
            if unlike:
                if not already_liked:
                    return True, ""
                btn = await _wait_visible(page, UNLIKE_BUTTON_SELECTORS, timeout_ms=5000)
            else:
                if already_liked:
                    return True, ""
                btn = await _wait_visible(page, LIKE_BUTTON_SELECTORS, timeout_ms=5000)

            if btn is None:
                return False, "未在推文详情页定位到点赞按钮"

            await notify_submit(on_submit)
            submitted = True
            await DEFAULT_HUMAN_ENGINE.click_locator(btn, page=page, timeout=3500)

            expected_sel = LIKE_BUTTON_SELECTORS if unlike else UNLIKE_BUTTON_SELECTORS
            deadline = asyncio.get_running_loop().time() + 12.0
            while asyncio.get_running_loop().time() < deadline:
                if confirmed:
                    return True, ""
                if api_err:
                    return False, api_err
                if (await _wait_visible(page, expected_sel, timeout_ms=500)) is not None:
                    return True, ""
                await human_wait(page, 500)

            return False, "write_uncertain:点赞请求已触发但未确认状态变更"
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

    try:
        return await _run_in_account_page(mgr, identity, headed=headed, fn=_drive)
    except Exception as exc:
        if submitted:
            return False, f"write_uncertain:X 点赞操作中断({exc})"
        return False, f"browser:{type(exc).__name__}:{exc}"


async def do_twitter_retweet(
    mgr: BrowserManager,
    identity: Identity,
    tweet_or_url: str,
    *,
    quote_text: str = "",
    unretweet: bool = False,
    headed: bool = True,
    on_submit: Optional[Callable[[], Any]] = None,
) -> Tuple[bool, str, str]:
    """执行转推 (Repost)、取消转推或带评论引用推文 (Quote)，返回 `(ok, result_id_or_ok, error)`。"""
    ref = resolve_twitter_tweet_ref(tweet_or_url)
    if ref is None:
        return False, "", "无效的 X 推文链接或推文 ID"
    q_body = trim_to_twitter_limit(str(quote_text or "").strip(), limit=280)
    submitted = False
    confirmed_id = ""
    api_err = ""

    async def _drive(page) -> Tuple[bool, str, str]:
        nonlocal submitted, confirmed_id, api_err

        async def on_response(resp):
            nonlocal confirmed_id, api_err
            u = str(resp.url or "")
            if not any(h in u for h in (*GQL_RETWEET_HINTS, *GQL_CREATE_TWEET_HINTS)):
                return
            if int(resp.status) >= 400:
                try:
                    payload = await resp.json()
                except Exception:
                    payload = None
                api_err = classify_twitter_payload_error(int(resp.status), payload)
                return
            try:
                payload = await resp.json()
                tweets, _ = extract_graphql_tweets(payload)
                if tweets and tweets[0].get("rest_id"):
                    confirmed_id = str(tweets[0]["rest_id"])
                    return
            except Exception:
                pass
            confirmed_id = "ok"

        page.on("response", on_response)
        try:
            await page.goto(ref.url, wait_until="domcontentloaded", timeout=35000)
            await human_wait(page, 2000)
            risk_err = check_twitter_url_risk(page.url)
            if risk_err:
                return False, "", risk_err

            already_rt = (
                await _wait_visible(page, UNRETWEET_BUTTON_SELECTORS, timeout_ms=2500)
            ) is not None
            if unretweet and not q_body:
                if not already_rt:
                    return True, "ok", ""
                btn = await _wait_visible(
                    page, UNRETWEET_BUTTON_SELECTORS, timeout_ms=5000
                )
                if btn is None:
                    return False, "", "未找到取消转推按钮"
                await DEFAULT_HUMAN_ENGINE.click_locator(btn, page=page, timeout=3000)
                await human_wait(page, 600)
                confirm = page.locator('[data-testid="unretweetConfirm"]').first
                await notify_submit(on_submit)
                submitted = True
                await DEFAULT_HUMAN_ENGINE.click_locator(
                    confirm, page=page, timeout=3500
                )
            elif q_body:
                rt_btn = await _wait_visible(
                    page,
                    (*RETWEET_BUTTON_SELECTORS, *UNRETWEET_BUTTON_SELECTORS),
                    timeout_ms=6000,
                )
                if rt_btn is None:
                    return False, "", "未找到转推/引用按钮"
                await DEFAULT_HUMAN_ENGINE.click_locator(
                    rt_btn, page=page, timeout=3000
                )
                await human_wait(page, 600)
                quote_item = await _wait_visible(
                    page, QUOTE_MENUITEM_SELECTORS, timeout_ms=5000
                )
                if quote_item is None:
                    return False, "", "未找到 Quote(引用) 菜单项"
                await DEFAULT_HUMAN_ENGINE.click_locator(
                    quote_item, page=page, timeout=3000
                )
                await human_wait(page, 900)
                box = await _wait_visible(
                    page, COMPOSE_TEXTBOX_SELECTORS, timeout_ms=8000
                )
                if box is None:
                    return False, "", "未找到引用推文输入框"
                from ..platforms.twitter.formatter import twitter_weighted_length
                from ..platforms.twitter.publish import (
                    _TRAILING_ENTITY_RE,
                    _click_tweet_submit_once,
                    _dismiss_compose_overlays,
                )

                to_type_q = (
                    q_body + " "
                    if (
                        _TRAILING_ENTITY_RE.search(q_body)
                        and twitter_weighted_length(q_body) + 1 <= 280
                    )
                    else q_body
                )
                await DEFAULT_HUMAN_ENGINE.type_text(
                    box, to_type_q, page=page, clear=False, timeout=4000
                )
                await human_wait(page, 500)
                await _dismiss_compose_overlays(page)
                send_btn = await _wait_visible(
                    page, COMPOSE_SUBMIT_BUTTON_SELECTORS, timeout_ms=8000
                )
                if send_btn is None:
                    return False, "", "引用推文发送按钮不可用"

                async def _mark_q():
                    nonlocal submitted
                    if not submitted:
                        submitted = True
                        await notify_submit(on_submit)

                await _click_tweet_submit_once(
                    page,
                    send_btn,
                    mark_submitted=_mark_q,
                    was_request_sent=lambda: bool(confirmed_id or api_err),
                )
            else:
                if already_rt:
                    return True, "ok", ""
                rt_btn = await _wait_visible(
                    page, RETWEET_BUTTON_SELECTORS, timeout_ms=6000
                )
                if rt_btn is None:
                    return False, "", "未找到转推按钮"
                await DEFAULT_HUMAN_ENGINE.click_locator(
                    rt_btn, page=page, timeout=3000
                )
                await human_wait(page, 600)
                confirm = await _wait_visible(
                    page, RETWEET_CONFIRM_SELECTORS, timeout_ms=5000
                )
                if confirm is None:
                    return False, "", "未找到确认转推菜单项"
                await notify_submit(on_submit)
                submitted = True
                await DEFAULT_HUMAN_ENGINE.click_locator(
                    confirm, page=page, timeout=3500
                )

            deadline = asyncio.get_running_loop().time() + 15.0
            while asyncio.get_running_loop().time() < deadline:
                if confirmed_id:
                    return True, confirmed_id, ""
                if api_err:
                    return False, "", api_err
                if not q_body and not unretweet:
                    if (
                        await _wait_visible(
                            page, UNRETWEET_BUTTON_SELECTORS, timeout_ms=500
                        )
                    ) is not None:
                        return True, "ok", ""
                await human_wait(page, 600)

            return False, "", "write_uncertain:转推/引用已提交但未收到明确确认，请人工核对"
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

    try:
        return await _run_in_account_page(mgr, identity, headed=headed, fn=_drive)
    except Exception as exc:
        if submitted:
            return False, "", f"write_uncertain:X 转推提交后中断({exc})"
        return False, "", f"browser:{type(exc).__name__}:{exc}"


async def do_twitter_follow(
    mgr: BrowserManager,
    identity: Identity,
    target_user: str,
    *,
    unfollow: bool = False,
    headed: bool = True,
    on_submit: Optional[Callable[[], Any]] = None,
) -> Tuple[bool, str]:
    """在目标用户主页执行关注或取关（具备已关注状态预检与二次确认框处理）。"""
    target = resolve_twitter_user_target(target_user) or str(target_user or "").strip().lstrip("@")
    if not target:
        return False, "缺少目标 X 用户名或数字 ID"

    submitted = False
    confirmed = False
    api_err = ""

    async def _drive(page) -> Tuple[bool, str]:
        nonlocal submitted, confirmed, api_err

        async def on_response(resp):
            nonlocal confirmed, api_err
            if not submitted:
                return
            u = str(resp.url or "")
            if not any(h in u for h in GQL_FOLLOW_MUTATION_HINTS):
                return
            if int(resp.status) < 400:
                confirmed = True
            else:
                try:
                    payload = await resp.json()
                except Exception:
                    payload = None
                api_err = classify_twitter_payload_error(int(resp.status), payload)

        page.on("response", on_response)
        try:
            await page.goto(profile_url(target), wait_until="domcontentloaded", timeout=35000)
            await human_wait(page, 2200)
            risk_err = check_twitter_url_risk(page.url)
            if risk_err:
                return False, risk_err

            is_following_now = (
                await _wait_visible(page, UNFOLLOW_BUTTON_SELECTORS, timeout_ms=2500)
            ) is not None
            if unfollow:
                if not is_following_now:
                    return True, ""
                btn = await _wait_visible(page, UNFOLLOW_BUTTON_SELECTORS, timeout_ms=5000)
                if btn is None:
                    return False, "未在主页定位到「正在关注」按钮"
                await DEFAULT_HUMAN_ENGINE.click_locator(btn, page=page, timeout=3000)
                await human_wait(page, 600)
                confirm_btn = await _wait_visible(
                    page, CONFIRM_SHEET_BUTTON_SELECTORS, timeout_ms=5000
                )
                if confirm_btn is None:
                    return False, "未弹出取关二次确认框"
                await notify_submit(on_submit)
                submitted = True
                await DEFAULT_HUMAN_ENGINE.click_locator(
                    confirm_btn, page=page, timeout=3500
                )
            else:
                if is_following_now:
                    return True, ""
                btn = await _wait_visible(page, FOLLOW_BUTTON_SELECTORS, timeout_ms=5000)
                if btn is None:
                    return False, "未在主页定位到 Follow(关注) 按钮（目标账号可能不存在或已屏蔽）"
                await notify_submit(on_submit)
                submitted = True
                await DEFAULT_HUMAN_ENGINE.click_locator(btn, page=page, timeout=3500)

            expected_sel = FOLLOW_BUTTON_SELECTORS if unfollow else UNFOLLOW_BUTTON_SELECTORS
            deadline = asyncio.get_running_loop().time() + 14.0
            while asyncio.get_running_loop().time() < deadline:
                if confirmed:
                    return True, ""
                if api_err:
                    return False, api_err
                if (await _wait_visible(page, expected_sel, timeout_ms=500)) is not None:
                    return True, ""
                await human_wait(page, 500)

            return False, "write_uncertain:关注/取关点击已执行，但未观测到按钮状态翻转，请人工核对"
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

    try:
        return await _run_in_account_page(mgr, identity, headed=headed, fn=_drive)
    except Exception as exc:
        if submitted:
            return False, f"write_uncertain:X 关注/取关提交后连接中断({exc})"
        return False, f"browser:{type(exc).__name__}:{exc}"


async def send_twitter_dm(
    mgr: BrowserManager,
    identity: Identity,
    target_user_or_conv: str,
    text: str,
    *,
    conv_id: str = "",
    headed: bool = True,
    on_submit: Optional[Callable[[], Any]] = None,
) -> Tuple[bool, str]:
    """向已有会话 `conv_id` 或指定 `@username` 发送 X 私信。"""
    msg_text = str(text or "").strip()
    if not msg_text:
        return False, "私信内容不能为空"

    cid = str(conv_id or "").strip()
    raw_target = str(target_user_or_conv or "").strip()
    # 形如 `12345-67890` 的双端会话 ID 直接当作 conv_id 打开
    if not cid and "-" in raw_target and all(p.isdigit() for p in raw_target.split("-", 1)):
        cid = raw_target

    submitted = False
    confirmed = False
    api_err = ""

    async def _drive(page) -> Tuple[bool, str]:
        nonlocal submitted, confirmed, api_err

        async def on_response(resp):
            nonlocal confirmed, api_err
            if not submitted:
                return
            u = str(resp.url or "")
            if not any(h in u for h in REST_DM_SEND_HINTS):
                return
            if int(resp.status) < 400:
                confirmed = True
            else:
                try:
                    payload = await resp.json()
                except Exception:
                    payload = None
                api_err = classify_twitter_payload_error(int(resp.status), payload)

        page.on("response", on_response)
        try:
            if cid:
                await page.goto(
                    dm_conversation_url(cid), wait_until="domcontentloaded", timeout=40000
                )
                await human_wait(page, 2400)
            else:
                user_target = resolve_twitter_user_target(raw_target) or raw_target.lstrip("@")
                if not user_target:
                    return False, "缺少私信接收方 @username、UID 或会话 ID"
                await page.goto(
                    profile_url(user_target), wait_until="domcontentloaded", timeout=38000
                )
                await human_wait(page, 2200)
                risk_err = check_twitter_url_risk(page.url)
                if risk_err:
                    return False, risk_err
                dm_btn = await _wait_visible(page, DM_PROFILE_BUTTON_SELECTORS, timeout_ms=7000)
                if dm_btn is None:
                    return False, "该 X 用户未开放陌生人私信入口（主页无发私信按钮）"
                await DEFAULT_HUMAN_ENGINE.click_locator(dm_btn, page=page, timeout=3500)
                await human_wait(page, 2000)

            risk_err = check_twitter_url_risk(page.url)
            if risk_err:
                return False, risk_err

            # 检查加密私信 PIN 拦截
            for pin_sel in DM_PIN_PROMPT_SELECTORS:
                loc = page.locator(pin_sel).first
                try:
                    if await loc.count() and await loc.is_visible():
                        return (
                            False,
                            "X 加密私信要求先解锁安全 PIN 码，请在账号页点击「打开浏览器」输入一次 PIN",
                        )
                except Exception:
                    continue

            composer = await _wait_visible(page, DM_COMPOSER_INPUT_SELECTORS, timeout_ms=10000)
            if composer is None:
                return False, "未找到 X 私信消息输入框"

            await DEFAULT_HUMAN_ENGINE.type_text(
                composer, msg_text, page=page, clear=False, timeout=4000
            )
            await human_wait(page, 500)

            send_btn = await _wait_visible(page, DM_SEND_BUTTON_SELECTORS, timeout_ms=5000)
            await notify_submit(on_submit)
            submitted = True
            if send_btn is not None:
                await DEFAULT_HUMAN_ENGINE.click_locator(
                    send_btn, page=page, timeout=3500
                )
            else:
                await page.keyboard.press("Enter")

            deadline = asyncio.get_running_loop().time() + 18.0
            snippet = msg_text[:24]
            while asyncio.get_running_loop().time() < deadline:
                if confirmed:
                    return True, ""
                if api_err:
                    return False, api_err
                try:
                    rendered = await page.evaluate(
                        """(needle) => {
                            const entries = document.querySelectorAll('[data-testid="messageEntry"]');
                            for (let i = entries.length - 1; i >= 0; i--) {
                                if ((entries[i].innerText || '').includes(needle)) return true;
                            }
                            return false;
                        }""",
                        snippet,
                    )
                    if rendered:
                        return True, ""
                except Exception:
                    pass
                await human_wait(page, 700)

            return False, "write_uncertain:私信已点击发送但未确认回执，请到 X 私信列表核对以免重发"
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

    try:
        return await _run_in_account_page(mgr, identity, headed=headed, fn=_drive)
    except Exception as exc:
        if submitted:
            return False, f"write_uncertain:X 私信提交后浏览器连接中断({exc})"
        return False, f"browser:{type(exc).__name__}:{exc}"
