"""本账号关注/取关与私信发送（API 优先 + 浏览器回退）执行器。"""
from __future__ import annotations

from typing import Any, Tuple

from app.browser.hub_common import _bili_cookie_str, _ig_cookie_str
from app.browser.manager import BrowserManager
from app.browser.xhs_dm import send_xhs_dm_page


# ═══════════ 写操作(无公开接口:登录态浏览器 UI 自动化) ═══════════
# 关注按钮的稳定锚点。注意别写成 user-info-follow(少个 -btn)——那是统计区
# 「关注 N」那一项,点下去只会打开关注列表,然后一路返回「成功」。
_FOLLOW_BTN_ANCHOR = '[data-e2e="user-info-follow-btn"]'


# 锚点缺失时的兜底。不放 span:has-text("关注"):顶栏「关注」feed 导航也匹配。
_FOLLOW_BTN_FALLBACK = [
    'button:has-text("已关注")', 'button:has-text("关注")',
    'div[role=button]:has-text("关注")', '.follow-button',
]


# 按钮文案 → 当前是否已关注。含「已/互相/相互」的先判,否则「已关注」会被「关注」吃掉。
_FOLLOWING_TEXTS = ("已关注", "相互关注", "互相关注")


_FOLLOW_TEXTS = ("关注", "回关")


_PROFILE_URL = {
    "douyin": "https://www.douyin.com/user/{sec}",
    "xhs": "https://www.xiaohongshu.com/user/profile/{uid}",
    "kuaishou": "https://www.kuaishou.com/profile/{uid}",
    "bilibili": "https://space.bilibili.com/{uid}",
    "instagram": "https://www.instagram.com/{sec}/",
    "twitter": "https://x.com/{sec}",
}


async def _open_target_profile(mgr, identity, platform, target_uid, target_sec_uid):
    sec = target_sec_uid or target_uid
    url = _PROFILE_URL.get(platform, _PROFILE_URL["douyin"])
    url = url.format(sec=sec, uid=target_uid or sec)
    ctx = (await mgr.context_for(identity) if platform == "xhs"
           else await mgr.open_headed(identity))
    page = (await mgr.new_page(identity, block_media=False)
            if platform == "xhs" else await ctx.new_page())
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    if platform == "xhs":
        try:
            await page.bring_to_front()
        except Exception:
            pass
        await mgr.xhs_interaction.pause(0.25, 0.55)
    else:
        await page.wait_for_timeout(1800)
    return ctx, page


def _is_following(text: str) -> bool:
    return any(t in text for t in _FOLLOWING_TEXTS)


async def _first_visible(page, sel, limit: int = 8):
    """取第一个可见匹配,不是第一个匹配。

    抖音主页同时挂两个 user-info-follow-btn:吸顶栏里那个副本 rect 全 0。
    原来用 .first 恒中隐藏副本,再被 is_visible() 判掉,最后报「未找到按钮」。
    """
    loc = page.locator(sel)
    for i in range(min(await loc.count(), limit)):
        cand = loc.nth(i)
        try:
            if await cand.is_visible():
                return cand
        except Exception:
            continue
    return None


async def _follow_button(page) -> Tuple[Any, str]:
    """返回(可见的关注按钮 locator, 按钮文案);找不到则 (None, "")。"""
    btn = await _first_visible(page, _FOLLOW_BTN_ANCHOR)
    if btn is None:
        for sel in _FOLLOW_BTN_FALLBACK:
            btn = await _first_visible(page, sel)
            if btn is not None:
                break
    if btn is None:
        return None, ""
    try:
        return btn, (await btn.inner_text()).strip()
    except Exception:
        return btn, ""


async def _await_follow_button(page, timeout_ms: int = 12000,
                               interaction=None) -> Tuple[Any, str]:
    """轮询等按钮出现:主页偶尔要好几秒才渲染出 user-info(空 title「的抖音」)。"""
    waited = 0
    while True:
        btn, text = await _follow_button(page)
        if btn is not None and text:
            return btn, text
        if waited >= timeout_ms:
            return btn, text
        if interaction is None:
            await page.wait_for_timeout(500)
        else:
            await interaction.pause(0.2, 0.45)
        waited += 500


async def _wait_flip(page, want_following: bool, timeout_ms: int = 6000,
                     interaction=None) -> str:
    """等按钮文案翻到目标态,返回最后读到的文案(超时则返回未翻转的文案)。"""
    waited, text = 0, ""
    while waited < timeout_ms:
        if interaction is None:
            await page.wait_for_timeout(400)
        else:
            await interaction.pause(0.18, 0.38)
        waited += 400
        _, text = await _follow_button(page)
        if text and _is_following(text) == want_following:
            return text
    return text


async def _dismiss_confirm(page, interaction=None) -> bool:
    """取关有时弹二次确认。只点按钮:'text=确定' 是子串匹配,会点中标题
    「确定要取消关注吗」这类纯文本节点,点了等于没点。"""
    for c in ("确定", "取消关注", "不再关注"):
        cc = await _first_visible(page, f'button:has-text("{c}")')
        if cc is not None:
            try:
                if interaction is None:
                    await cc.click(timeout=2500)
                else:
                    await interaction.click_visible(cc)
                return True
            except Exception:
                continue
    return False


async def do_follow(mgr: BrowserManager, identity, platform: str, target_uid: str = "",
                    target_sec_uid: str = "", unfollow: bool = False, *,
                    _xhs_visible: bool = False
                    ) -> Tuple[bool, str]:
    """打开目标主页,点「关注 / 已关注」按钮(UI 自动化,有头窗口更稳)。

    按钮文案就是当前关注态,先读再决定点不点:已是目标态直接成功返回(幂等),
    点完轮询等文案翻转才算成功 —— 只确认「点到了东西」会把空点、二次确认框、
    被风控拦下统统算成成功。

    首次点击常常打空:按钮已渲染但 React handler 还没绑上(和粉丝/作品/私信
    入口同一个 hydrate 病)。所以点不动就重点,而不是直接判失败。
    """
    if platform == "rednote":
        return False, "unavailable:海外小红书暂未开放自动关注/取关"
    if platform == "twitter":
        from app.browser.twitter_actions import do_twitter_follow
        return await do_twitter_follow(
            mgr, identity, target_sec_uid or target_uid, unfollow=unfollow
        )
    if platform == "instagram":
        from app.platforms.instagram import InstagramClient
        cookie_s = await _ig_cookie_str(mgr, identity)
        if not cookie_s:
            return False, "logged_out:账号未登录"
        client = InstagramClient(
            cookie_s,
            getattr(identity, "ua", "") or getattr(identity, "user_agent", ""),
            proxy=getattr(identity, "proxy", ""),
            account_seed=getattr(identity, "fp_seed", "") or getattr(identity, "profile_dir", ""),
            timezone_id=getattr(identity, "timezone_id", "Asia/Shanghai"),
        )
        async with client.session_scope():
            return await client.modify_follow(
                target_uid or target_sec_uid, follow=not unfollow
            )
    if platform == "bilibili":
        from app.platforms.bilibili import BilibiliClient
        cookie_s = await _bili_cookie_str(mgr, identity)
        if not cookie_s:
            return False, "logged_out:账号未登录"
        client = BilibiliClient(
            cookie_s,
            getattr(identity, "user_agent", ""),
            proxy=getattr(identity, "proxy", ""),
        )
        async with client.session_scope():
            return await client.modify_follow(
                target_uid or target_sec_uid, follow=not unfollow
            )
    if platform == "xhs" and not _xhs_visible:
        async with mgr.visible_action(identity):
            return await do_follow(
                mgr, identity, platform, target_uid, target_sec_uid, unfollow,
                _xhs_visible=True)
    ctx = None
    page = None
    want = "取关" if unfollow else "关注"
    want_following = not unfollow
    try:
        ctx, page = await _open_target_profile(mgr, identity, platform,
                                               target_uid, target_sec_uid)
        if "passport" in page.url or "/login" in page.url:
            return False, "logged_out:账号未登录"

        interaction = mgr.xhs_interaction if platform == "xhs" else None
        btn, text = await _await_follow_button(
            page, interaction=interaction)
        if btn is None or not text:
            return False, f"未找到关注按钮(主页未渲染/改版?url={page.url})"
        if _is_following(text) == want_following:      # 已是目标状态
            return True, ""

        after = text
        if platform == "xhs":
            try:
                await interaction.click_visible(btn)
            except Exception as e:
                return False, f"{want}点击失败: {e!r}"
            if unfollow:
                await interaction.pause(0.2, 0.45)
                await _dismiss_confirm(page, interaction=interaction)
            after = await _wait_flip(
                page, want_following, interaction=interaction)
            if after and _is_following(after) == want_following:
                return True, ""
            return False, (f"{want}结果未确认:按钮仍是「{after}」;"
                           "为避免重复操作，本次不再点击")
        for attempt in range(3):
            try:
                await btn.click(timeout=4000)
            except Exception as e:
                if attempt == 2:
                    return False, f"{want}点击失败: {e!r}"
                await page.wait_for_timeout(800)
                continue
            if unfollow:
                await page.wait_for_timeout(600)
                await _dismiss_confirm(page)
            after = await _wait_flip(page, want_following)
            if after and _is_following(after) == want_following:
                return True, ""
            # 空点:重新取一次 locator(翻转失败时 DOM 可能已被 React 换掉)
            btn, cur = await _follow_button(page)
            if btn is None:
                return False, f"{want}后按钮消失(点前「{text}」)"
            after = cur
        return False, f"{want}未生效:点了 3 次,按钮仍是「{after}」(风控?)"
    except Exception as e:
        return False, f"{want}异常: {e!r}"
    finally:
        try:
            if page is not None:
                await page.close()
            if ctx is not None and platform != "xhs":
                await ctx.close()
        except Exception:
            pass


_DM_INPUT = [
    'textarea[placeholder*="发送"]', 'div[contenteditable="true"][placeholder*="发送"]',
    'textarea[placeholder*="私信"]', 'div[contenteditable="true"]',
    'textarea', 'input[type="text"]',
]


_DM_SEND = ['button:has-text("发送")', 'span:has-text("发送")', '.send-btn']


_DM_ENTRY_URL = {
    "douyin": "https://www.douyin.com/user/{sec}",
    "xhs": "https://www.xiaohongshu.com/chat/{uid}",
    "kuaishou": "https://www.kuaishou.com/profile/{uid}",
    "bilibili": "https://message.bilibili.com/#/whisper/mid{uid}",
    "instagram": "https://www.instagram.com/direct/inbox/",
    "twitter": "https://x.com/messages",
}


_SEND_URL = "https://imapi.douyin.com/v1/message/send"


async def send_dm_api(mgr: BrowserManager, identity, conv_id: str,
                      conv_short_id: str, ticket: str, text: str,
                      conv_type: int = 1) -> Tuple[bool, str]:
    """抖音无头发私信:imapi/v1/message/send(cmd 100),cookie POST(先按零签名试,
    与读/ mark_read 一致)。需已有会话的 conv_id + short_id + ticket(同步会话列表时已存库)。"""
    import time
    import uuid as _uuid
    from app.browser.douyin_im_pb import build_send_request, parse_send_response
    text = (text or "").strip()
    if not text:
        return False, "空内容"
    if not (conv_id and conv_short_id and ticket):
        return False, "缺 conv_id/short_id/ticket(先同步会话列表)"
    try:
        ctx = await mgr.context_for(identity)
        cmid = str(_uuid.uuid4())
        stime = int(time.time() * 1000)
        req = build_send_request(conv_id, int(conv_type or 1), int(conv_short_id),
                                 ticket, text, cmid, stime)
        resp = await ctx.request.post(
            _SEND_URL, data=req,
            headers={"accept": "application/x-protobuf",
                     "content-type": "application/x-protobuf",
                     "referer": "https://www.douyin.com/"})
        body = await resp.body()
        r = parse_send_response(body)
        print(f"[dm-send] conv={conv_id} status={resp.status} "
              f"ok={r['ok']} msg={r['msg']!r} code={r['error_code']} resp_len={len(body)}")
        if resp.status == 200 and r["ok"] and r["cmd"] == 100:
            return True, ""
        if resp.status == 200 and r["ok"]:
            return False, "write_uncertain:发送回包命令不匹配"
        return False, f"发送被拒 status={resp.status} msg={r['msg']} code={r['error_code']}"
    except Exception as e:
        return False, f"发送失败: {e!r}"


async def send_dm(mgr: BrowserManager, identity, platform: str, target_uid: str = "",
                   target_sec_uid: str = "", text: str = "", *,
                   on_submit: Any = None,
                   _xhs_visible: bool = False) -> Tuple[bool, str]:
    """给目标发私信(UI 自动化):打开对方主页 → 点「私信」→ 输入 → 发送。
    ⚠️ 各平台私信入口/选择器差异大,首版尽力而为,失败有诊断。"""
    text = (text or "").strip()
    if not text:
        return False, "空内容"
    sec = target_sec_uid or target_uid
    if platform == "rednote":
        return False, "unavailable:海外小红书暂未开放自动私信发送"
    if platform == "twitter":
        from app.browser.twitter_actions import send_twitter_dm
        return await send_twitter_dm(
            mgr, identity, target_sec_uid or target_uid, text, on_submit=on_submit
        )
    url = _DM_ENTRY_URL.get(platform, _DM_ENTRY_URL["douyin"]).format(
        sec=sec, uid=target_uid or sec)
    if platform == "instagram":
        from app.platforms.instagram import InstagramClient
        cookie_s = await _ig_cookie_str(mgr, identity)
        if not cookie_s:
            return False, "logged_out:缺少 Instagram 登录 Cookie"
        client = InstagramClient(
            cookie_s,
            getattr(identity, "ua", "") or getattr(identity, "user_agent", ""),
            proxy=getattr(identity, "proxy", ""),
            account_seed=getattr(identity, "fp_seed", "") or getattr(identity, "profile_dir", ""),
            timezone_id=getattr(identity, "timezone_id", "Asia/Shanghai"),
        )
        async with client.session_scope():
            ok, err, _meta = await client.send_dm(
                target_uid or target_sec_uid, text, on_submit=on_submit
            )
            return ok, err
    if platform == "bilibili":
        from app.platforms.bilibili import BilibiliClient
        cookie_s = await _bili_cookie_str(mgr, identity)
        if not cookie_s:
            return False, "logged_out:缺少 B 站登录 Cookie"
        client = BilibiliClient(
            cookie_s,
            getattr(identity, "user_agent", ""),
            proxy=getattr(identity, "proxy", ""),
        )
        async with client.session_scope():
            ok, err, _meta = await client.send_dm(
                target_uid or target_sec_uid, text, on_submit=on_submit
            )
            return ok, err
    if platform == "xhs" and not _xhs_visible:
        async def _send_on_page(page):
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await mgr.xhs_interaction.pause(0.25, 0.55)
            if "passport" in page.url or "/login" in page.url:
                return False, "logged_out:账号未登录"
            return await send_xhs_dm_page(
                mgr, page, text, on_submit=on_submit)

        visible_page = getattr(mgr, "visible_page", None)
        if callable(visible_page):
            try:
                lease = visible_page(identity, foreground=False)
            except TypeError:  # compatibility with compact manager stubs
                lease = visible_page(identity)
            try:
                async with lease as page:
                    return await _send_on_page(page)
            except Exception as exc:
                return False, f"发私信异常: {exc!r}"

        # Compatibility path for older manager implementations: still one
        # temporary page, while current BrowserManager always takes the
        # resident-page branch above.
        async with mgr.visible_action(identity):
            page = None
            try:
                page = await mgr.new_page(identity, block_media=False)
                return await _send_on_page(page)
            except Exception as exc:
                return False, f"发私信异常: {exc!r}"
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass
    ctx = None
    page = None
    try:
        ctx = (await mgr.context_for(identity) if platform == "xhs"
               else await mgr.open_headed(identity))
        page = (await mgr.new_page(identity, block_media=False)
                if platform == "xhs" else await ctx.new_page())
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if platform == "xhs":
            try:
                await page.bring_to_front()
            except Exception:
                pass
            await mgr.xhs_interaction.pause(0.25, 0.55)
        else:
            await page.wait_for_timeout(1800)
        if "passport" in page.url or "/login" in page.url:
            return False, "logged_out:账号未登录"
        if platform == "xhs":
            return await send_xhs_dm_page(
                mgr, page, text, on_submit=on_submit)
        # 点开「私信」入口
        opened = False
        for label in ("私信", "发消息", "发私信"):
            try:
                el = page.get_by_text(label, exact=False).first
                if await el.count():
                    if platform == "xhs":
                        await mgr.xhs_interaction.click_visible(el)
                    else:
                        await el.click(timeout=4000)
                    opened = True
                    if platform == "xhs":
                        await mgr.xhs_interaction.pause(0.25, 0.55)
                    else:
                        await page.wait_for_timeout(1500)
                    break
            except Exception:
                continue
        editor = None
        for sel in _DM_INPUT:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    editor = loc
                    break
            except Exception:
                continue
        if editor is None:
            return False, ("未找到私信输入框(私信入口可能需手动打开/页面改版)。"
                           f"opened_entry={opened}")
        if platform == "xhs":
            if len(text) <= 40:
                await mgr.xhs_interaction.type_short(editor, text)
            else:
                await mgr.xhs_interaction.insert_long(
                    editor, text, page=page)
        else:
            await editor.click(timeout=8000)
            await page.keyboard.type(text, delay=35)
            await page.wait_for_timeout(500)
        sent = False
        for sel in _DM_SEND:
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_enabled():
                    if platform == "xhs":
                        await mgr.xhs_interaction.click_visible(btn)
                    else:
                        await btn.click(timeout=3000)
                    sent = True
                    break
            except Exception:
                continue
        if not sent:
            try:
                await page.keyboard.press("Enter")
                sent = True
            except Exception:
                pass
        if platform == "xhs":
            await mgr.xhs_interaction.pause(0.25, 0.55)
        else:
            await page.wait_for_timeout(1200)
        return (sent, "" if sent else "未找到发送方式")
    except Exception as e:
        return False, f"发私信异常: {e!r}"
    finally:
        try:
            if page is not None:
                await page.close()
            if ctx is not None and platform != "xhs":
                await ctx.close()
        except Exception:
            pass
