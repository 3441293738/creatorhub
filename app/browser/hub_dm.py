"""多平台私信会话列表、消息历史抓取及用户资料补全。"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple
import json
import os

from app.browser.douyin_im_pb import parse_conversations
from app.browser.hub_common import _ID_KEYS, _NAME_KEYS, _STRONG_ID_KEYS, _avatar_of, _bili_cookie_str, _ig_cookie_str, _looks_like_user, _twitter_cookie_str
from app.browser.manager import BrowserManager


# ═══════════ 私信(无公开接口:拦截私信页 XHR;发送走 UI 自动化) ═══════════
_DM_NAV = {
    # 抖音私信(IM)接口(/v1/message/、/v1/stranger/)在主站/关注页就会自动加载,
    # 打开 /follow 最稳(实测会触发 get_conversation_list / get_message_by_init)
    "douyin":   "https://www.douyin.com/follow",
    # 小红书:/im 直开会 goto 失败(api_seen=0)。回到首页(能正常加载并触发 message/entry),
    # 靠采样 entry 响应体 + 点私信入口来标定
    "xhs":      "https://www.xiaohongshu.com/chat",
    "kuaishou": "https://www.kuaishou.com/",
}


# 私信/会话接口的 URL 关键词(抖音实测:/v1/message/、/v1/stranger/get_conversation_list)。
# 注意排除 /aweme/v1/web/im/{resources,active,strategy} 这类「IM 配置/表情」噪声接口。
# 小红书实测 api_seen 有 /api/im/web/message/entry —— 说明其私信走 web REST(非纯 WS),
# 把 /api/im/web/ 纳入命中,便于采样真实会话列表接口(排除 entry/version 这类配置噪声)。
_DM_URL_HINTS = ("/v1/message/", "/v1/stranger/", "get_conversation_list",
                 "get_message_by_init", "get_user_message", "/imapi/",
                 "/im/conversation", "conversation/list",
                 # 抖音 IM 真实端点(protobuf,实测标定):建会话/拉会话列表/发消息
                 "imapi.douyin.com", "/v2/conversation/create",
                 "/v2/conversation/get_info_list", "/v1/message/send",
                 "/api/im/web/chat", "/api/im/web/session", "/api/im/web/conversation",
                 "/api/im/web/msg", "/api/im/web/v3/chats",
                 "/api/im/web/messages/history")


# 「私信面板真的打开了」的信号:只认会话/消息数据接口。
# 不含 im/strategy/config、im/resources/*、im/user/active/* 等 —— 它们随便哪个页面加载都会发,
# 拿来当信号会把「SDK 预加载」误判成「面板已打开」。
_IM_BOOTSTRAP_SIGNALS = ("/v1/message/", "/v1/stranger/", "get_message_by_init",
                         "get_conversation_list", "/imapi/", "/v2/conversation/",
                         "/aweme/v1/web/im/user/info", "/api/im/web/chat",
                         "/api/im/web/session", "/api/im/web/msg",
                         "/api/im/web/v3/chats", "/api/im/web/messages/history")


# 抖音顶栏「消息」是 <div>(无 href),React onClick 绑在祖先上。
# ⚠️ 向上找可点祖先只用来判定「这一项可点」,坐标必须取文本元素自身的中心 ——
#    祖先常是整条导航栏容器,取它的中心会点到旁边的空白或别的 nav item。
_DOUYIN_IM_BOXES_JS = """() => {
  const hits = [...document.querySelectorAll('div,span,a,button,li')]
    .filter(e => (e.textContent || '').trim() === '消息');
  const out = [];
  for (const e of hits) {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    let clickable = false;
    for (let t = e, d = 0; d < 4 && t; d++, t = t.parentElement) {
      const s = getComputedStyle(t);
      if (t.tagName === 'A' || t.tagName === 'BUTTON'
          || t.getAttribute('role') === 'button' || s.cursor === 'pointer') {
        clickable = true; break;
      }
    }
    const x = r.x + r.width / 2, y = r.y + r.height / 2;
    const top = document.elementFromPoint(x, y);   // 该坐标上真正会收到点击的元素
    const covered = !(top && (top === e || e.contains(top) || top.contains(e)));
    out.push({x, y, tag: e.tagName, clickable, covered});
  }
  return out;
}"""


def _peer_uid_from_conv_id(conv_id: str, self_uid: str = "") -> str:
    """抖音单聊 conversation_id 格式为 `0:{type}:{uidA}:{uidB}`。
    实测两个 uid 的顺序不固定(见到过 `0:1:{peer}:{me}`,对端在前),所以
    只能靠「不是本账号 uid 的那个」判定对端 —— 必须已知 self_uid,否则返回空不猜。"""
    if not conv_id or ":" not in conv_id or not self_uid:
        return ""
    segs = [s for s in conv_id.split(":") if s.isdigit()]
    uids = [s for s in segs if len(s) >= 6]   # uid 远长于 type/idx 段
    if self_uid not in uids:
        return ""
    return next((u for u in uids if u != self_uid), "")


def _dm_last_text(content) -> str:
    """抖音消息 content 是一段 JSON 字符串,文本在 .text(见 douyin_recv_msg.py)。
    兼容:已是 dict、纯文本、或 {text}/{content} 的 JSON 串。"""
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    if isinstance(content, str):
        s = content.strip()
        if s.startswith("{"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    return str(obj.get("text") or obj.get("content") or "")
            except Exception:
                pass
        return content
    return ""


def _norm_conv(d: dict) -> Optional[dict]:
    """从会话对象兜底抽 {conv_id, peer_uid, peer_nickname, peer_avatar, last_text, last_time}。"""
    if not isinstance(d, dict):
        return None
    # ── imapi/protobuf 分支:抖音 v2/conversation/get_info_list 解出的会话只有
    #    conversation_id / conversation_short_id / conversation_type / ticket,
    #    没有内嵌 user 对象。此时从 conv_id 拆 peer_uid,昵称/头像留空待二次水合。
    _conv_id = str(d.get("conversation_id") or d.get("conversationId") or "")
    if _conv_id and int(d.get("conversation_type") or 0) == 1 \
            and not (d.get("user") or d.get("fromUser") or d.get("peer")):
        peer = _peer_uid_from_conv_id(_conv_id)
        if peer:
            return {
                "conv_id": _conv_id,
                "peer_uid": peer,
                "peer_sec_uid": "",
                "peer_nickname": "",          # protobuf 会话不带昵称,需二次水合
                "peer_avatar": "",
                "last_text": _dm_last_text(d.get("content") or d.get("last_message")),
                "last_time": int(d.get("last_time") or 0),
                "unread_count": int(d.get("unread") or d.get("badge_count") or 0),
                "conv_short_id": str(d.get("conversation_short_id") or ""),
                "ticket": str(d.get("ticket") or ""),
            }
    peer = d.get("user") or d.get("fromUser") or d.get("toUser") or d.get("peer") or d
    if not isinstance(peer, dict):
        return None
    # peer 必须像用户(有头像或强 id),否则是 JS 模块/无关对象({id,name})
    if not (_avatar_of(peer) or any(peer.get(k) for k in _STRONG_ID_KEYS)):
        return None
    # 且整条记录要有「会话特征」:会话 id 或最近消息字段(纯用户对象不算会话)
    has_conv_marker = any(d.get(k) for k in (
        "conversation_id", "conversationId", "conv_id", "last_message",
        "lastMessage", "last_msg", "unread", "unreadCount"))
    nickname = ""
    for k in _NAME_KEYS:
        if peer.get(k):
            nickname = str(peer[k]); break
    conv_id = str(d.get("conversation_id") or d.get("conversationId")
                  or d.get("conv_id") or d.get("id") or "")
    peer_uid = ""
    for k in _ID_KEYS:
        if peer.get(k):
            peer_uid = str(peer[k]); break
    if not nickname or not (conv_id or peer_uid) or not has_conv_marker:
        return None
    last = d.get("last_message") or d.get("lastMessage") or {}
    # 抖音 last_message.content 可能是 JSON 串({text}),用统一解析
    last_text = _dm_last_text(last.get("content") or last.get("text") or "") \
        if isinstance(last, dict) else _dm_last_text(last)
    return {
        "conv_id": conv_id or peer_uid,
        "peer_uid": peer_uid,
        "peer_sec_uid": str(peer.get("sec_uid") or ""),
        "peer_nickname": nickname,
        "peer_avatar": _avatar_of(peer),
        "last_text": last_text,
        "last_time": int(d.get("last_time") or d.get("updateTime") or 0),
        "unread_count": int(d.get("unread") or d.get("unreadCount") or 0),
    }


def _harvest_users(data, into: Dict[str, dict]) -> None:
    """从任意 JSON 里递归捞「用户对象」,建 uid -> {nickname, avatar, sec_uid} 映射。
    抖音 /aweme/v1/web/im/user/info/ 返回的用户资料用来给 protobuf 会话水合昵称/头像。"""
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if _looks_like_user(cur):
                uid = ""
                for k in _ID_KEYS:
                    if cur.get(k):
                        uid = str(cur[k]); break
                if uid:
                    nickname = ""
                    for k in _NAME_KEYS:
                        if cur.get(k):
                            nickname = str(cur[k]); break
                    prev = into.get(uid, {})
                    into[uid] = {
                        "nickname": nickname or prev.get("nickname", ""),
                        "avatar": _avatar_of(cur) or prev.get("avatar", ""),
                        "sec_uid": str(cur.get("sec_uid") or cur.get("secUid")
                                       or prev.get("sec_uid", "")),
                    }
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


_DM_PANEL_PROBE_JS = """() => {
  const txt = document.body.innerText || '';
  const cls = (e) => (e.className && e.className.toString) ? e.className.toString() : '';
  // 抖音的 class 是编译后的 hash(ASay5JTn),靠 class 正则永远匹配不到 —— 认 data-e2e
  const imish = [...document.querySelectorAll('div,section,aside')]
    .filter(e => /im[-_]|message|conversation|chat|私信/i.test(cls(e))
              || /im|message|conversation/i.test(e.getAttribute('data-e2e') || ''))
    .filter(e => { const r = e.getBoundingClientRect(); return r.width > 200 && r.height > 200; });
  return {
    url: location.pathname,
    iframes: document.querySelectorAll('iframe').length,
    // 大块的 im/会话 容器出现 = 面板真的渲染了
    panels: imish.length,
    panel_cls: imish.slice(0, 3).map(e => cls(e).slice(0, 40)),
    // 面板里才会出现的文案
    has_dm_text: /私信|发消息|暂无消息|陌生人消息/.test(txt),
    dialogs: document.querySelectorAll('[role="dialog"],[class*="modal"],[class*="mask"]').length,
  };
}"""


async def _dm_panel_probe(page) -> dict:
    """点击前后各拍一次:面板到底渲染了没。比反复猜「点没点中」有用。"""
    try:
        return await page.evaluate(_DM_PANEL_PROBE_JS)
    except Exception as e:
        return {"probe_failed": repr(e)}


async def _fetch_im_user_info(page, sec_ids: list) -> Dict[str, dict]:
    """在 douyin 页面内批量 POST /aweme/v1/web/im/user/info/(body sec_user_ids=[...]),
    抖音自己的 fetch 拦截器会补 a_bogus 签名。返回 sec_uid -> {nickname, avatar, sec_uid}。"""
    ids = list(dict.fromkeys([s for s in sec_ids if s]))   # 去重
    if not ids:
        return {}
    try:
        data = await page.evaluate(
            """async (ids) => {
              const out = [];
              for (let i=0; i<ids.length; i+=20) {
                const chunk = ids.slice(i, i+20);
                const body = 'sec_user_ids=' + encodeURIComponent(JSON.stringify(chunk));
                try {
                  const r = await fetch('/aweme/v1/web/im/user/info/', {
                    method:'POST', credentials:'include',
                    headers:{'content-type':'application/x-www-form-urlencoded'}, body});
                  const j = await r.json();
                  if (j && Array.isArray(j.data)) out.push(...j.data);
                } catch (e) {}
              }
              return out;
            }""", ids)
    except Exception as e:
        print(f"[dm-userinfo] in-page fetch failed: {e!r}")
        return {}
    prof: Dict[str, dict] = {}
    for u in (data or []):
        if not isinstance(u, dict):
            continue
        sec = str(u.get("sec_uid") or u.get("secUid") or "")
        if not sec:
            continue
        nick = str(u.get("nickname") or u.get("alias_nickname") or "")
        avatar = _avatar_of(u)
        prof[sec] = {"nickname": nick, "avatar": avatar, "sec_uid": sec}
    print(f"[dm-userinfo] batch fetched {len(prof)}/{len(ids)} profiles")
    return prof


async def fetch_dm_conversations(mgr: BrowserManager, identity, platform: str,
                                 settle_ms: int = 2600, max_scrolls: int = 8,
                                 *, _xhs_visible: bool = False
                                 ) -> Tuple[List[dict], str]:
    """打开私信页,拦截会话列表 XHR,启发式抽会话。无公开接口,首版用于标定。"""
    if platform == "rednote":
        return [], "unavailable:海外小红书暂未开放私信协议通道"
    if platform == "twitter":
        from app.platforms.twitter import TwitterClient
        from app.browser.twitter_fetcher import fetch_twitter_dm_conversations
        cookie_s = await _twitter_cookie_str(mgr, identity)
        self_uid = ""
        if cookie_s:
            client = TwitterClient(
                cookie_s,
                getattr(identity, "ua", "") or getattr(identity, "user_agent", ""),
                proxy=getattr(identity, "proxy", ""),
            )
            self_uid = client.self_uid
            async with client.session_scope():
                rows, err = await client.fetch_dm_inbox()
                if rows or err.startswith(("logged_out:", "verification_required:")):
                    return rows, err
        return await fetch_twitter_dm_conversations(mgr, identity, self_uid=self_uid)
    if platform == "instagram":
        from app.platforms.instagram import InstagramClient
        cookie_s = await _ig_cookie_str(mgr, identity)
        if not cookie_s:
            return [], "logged_out:缺少 Instagram 登录 Cookie"
        client = InstagramClient(
            cookie_s,
            getattr(identity, "ua", "") or getattr(identity, "user_agent", ""),
            proxy=getattr(identity, "proxy", ""),
            account_seed=getattr(identity, "fp_seed", "") or getattr(identity, "profile_dir", ""),
            timezone_id=getattr(identity, "timezone_id", "Asia/Shanghai"),
        )
        async with client.session_scope():
            return await client.fetch_dm_conversations()
    if platform == "bilibili":
        from app.platforms.bilibili import BilibiliClient
        cookie_s = await _bili_cookie_str(mgr, identity)
        if not cookie_s:
            return [], "logged_out:缺少 B 站登录 Cookie"
        client = BilibiliClient(
            cookie_s,
            getattr(identity, "user_agent", ""),
            proxy=getattr(identity, "proxy", ""),
        )
        async with client.session_scope():
            return await client.fetch_dm_sessions(size=100)
    if platform == "xhs":
        from app.browser.xhs_im import fetch_conversations as fetch_xhs_conversations
        return await fetch_xhs_conversations(
            mgr, identity, settle_ms=settle_ms, _visible=_xhs_visible)
    if platform == "xhs" and not _xhs_visible:
        async with mgr.visible_action(identity):
            return await fetch_dm_conversations(
                mgr, identity, platform, settle_ms, max_scrolls,
                _xhs_visible=True)
    url = _DM_NAV.get(platform, _DM_NAV["douyin"])
    host = {"douyin": "douyin.com", "xhs": "xiaohongshu.com",
            "kuaishou": "kuaishou.com"}.get(platform, "douyin.com")
    collected: Dict[str, dict] = {}
    hit_urls: list = []
    api_seen: list = []
    dm_samples: list = []        # 命中私信接口的响应体样本(标定关键:看真实字段)
    dm_seen_paths: Set[str] = set()   # 同一接口只采一份样本,别被重复请求占满
    im_visible: Optional[bool] = None  # 小红书 message/entry 的 visible(false=网页端私信未开放)
    ws_urls: list = []           # 标定:抖音私信可能走 frontier-im WebSocket,XHR 拦截抓不到
    ws_frames: list = []         # 抓少量 WS 帧(仅 frontier-im),看会话/消息是不是走 WS 推
    im_hit = [False]             # IM 是否真的 bootstrap(点入口后据此确认,而非只看「点了」)
    dm_init_raw = [b""]          # 抖音:get_message_by_init 的 protobuf 大包(会话全在这)
    dm_init_request_raw = [b""]  # 对应请求体,用于离线标定/验证纯协议初始化
    im_profiles: Dict[str, dict] = {}  # uid -> {nickname, avatar, sec_uid},来自 im/user/info JSON
    error = ""
    page = await mgr.new_page(identity, block_media=platform != "xhs")
    if platform == "xhs":
        try:
            await page.bring_to_front()
        except Exception:
            pass

    # ── WebSocket 探针(标定关键):若会话列表走 frontier-im WS,则 page.on("response")
    #    永远抓不到 —— 这一行日志能直接判定抖音私信的传输形态。
    def on_websocket(ws):
        ws_urls.append(ws.url)

        def _cap(payload, direction):
            # bytelink 是直播心跳,会把配额刷满(上轮 10 帧里 7 帧是它),挤掉 frontier-im
            noisy = "bytelink" in ws.url
            if len(ws_frames) >= 20 or (noisy and sum(
                    1 for f in ws_frames if "bytelink" in f) >= 3):
                return
            try:
                n = len(payload) if payload is not None else 0
            except Exception:
                n = -1
            ws_frames.append(f"{direction}[{n}] {ws.url.split('?')[0].split('//')[-1][:34]}")
        ws.on("framereceived", lambda p: _cap(p, "recv"))
        ws.on("framesent", lambda p: _cap(p, "sent"))

    page.on("websocket", on_websocket)

    async def on_response(resp):
        nonlocal im_visible
        u = resp.url
        if host not in u or resp.request.resource_type not in ("xhr", "fetch"):
            return
        path = u.split("?")[0].split(host)[-1]
        low = path.lower()
        if len(api_seen) < 100:
            api_seen.append(f"{resp.status} {path}")
        # IM 真正起来的信号:必须是「会话/消息数据」接口。
        # ⚠️ 别用宽泛的 "/aweme/v1/web/im/" —— im/strategy/config、im/resources/emoticon/trending、
        # im/user/active/status 在任意页面加载时都会发,拿它当信号会误判成「面板已打开」。
        if any(s in low for s in _IM_BOOTSTRAP_SIGNALS):
            im_hit[0] = True
        # 抖音会话大包:get_message_by_init(protobuf)。留最大的一份(全量那次)。
        if platform == "douyin" and "get_message_by_init" in low:
            request_body = resp.request.post_data_buffer or b""
            if len(request_body) > len(dm_init_request_raw[0]):
                dm_init_request_raw[0] = request_body
            try:
                b = await resp.body()      # body() 内部已等 body 下完,无需 finished()
            except Exception as e:
                b = b""
                print(f"[dm-init] body 读取失败: {e!r}")
            # 一次同步会收到多份(增量包),只在换成更大的那份时打日志
            if len(b) > len(dm_init_raw[0]):
                dm_init_raw[0] = b
                print(f"[dm-init] get_message_by_init kept={len(b)} bytes")
        # 私信接口可能是 protobuf:先取文本,JSON 解析失败也留个样本(标注 non-json)
        raw_text = ""
        try:
            raw_text = await resp.text()
        except Exception:
            pass
        try:
            data = await resp.json()
        except Exception:
            data = None
        # 抖音:im/user/info(JSON)带用户资料,建 uid->昵称/头像映射给 protobuf 会话水合
        if platform == "douyin" and "/aweme/v1/web/im/user/info/" in low \
                and data is not None:
            _harvest_users(data, im_profiles)
        # 小红书 message/entry 的 visible 决定网页端私信是否开放(false=未开放,仅 App)
        if "/api/im/web/message/entry" in low and isinstance(data, dict):
            v = (data.get("data") or {}).get("visible")
            if isinstance(v, bool):
                im_visible = v
        # 命中私信接口留样本。小红书标定期:采样所有 /api/im/web/*(含 message/entry ——
        # 上轮它是唯一发出的 im 接口,其响应体可能就含会话/入口线索),仅排除表情/版本噪声。
        _im_calib = ("/api/im/web/" in low
                     and not any(n in low for n in ("version", "redmoji")))
        if (any(h in low for h in _DM_URL_HINTS) or _im_calib) \
                and low not in dm_seen_paths and len(dm_samples) < 10:
            dm_seen_paths.add(low)
            if data is not None:
                dm_samples.append(low + " => " + str(data)[:1400])
            else:
                # protobuf 二进制:resp.text() 对二进制会抛异常(记成 len=0 是假象),
                # 必须用 resp.body() 拿 bytes;base64 前 600 字节留样,离线解 protobuf 结构。
                import base64 as _b64
                try:
                    body = await resp.body()
                except Exception:
                    body = b""
                dm_samples.append(
                    f"{low} => [PROTOBUF,len={len(body)}] b64:"
                    + _b64.b64encode(body[:600]).decode())
        if data is None:
            return
        # 会话列表:找含 conversation/last_message 字段的数组
        before = len(collected)
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, list):
                for x in cur:
                    c = _norm_conv(x) if isinstance(x, dict) else None
                    if c:
                        collected[c["conv_id"]] = c
                    if isinstance(x, (list, dict)):
                        stack.append(x)
            elif isinstance(cur, dict):
                stack.extend(cur.values())
        if len(collected) > before and path not in hit_urls:
            hit_urls.append(path)

    # IM 会话/消息接口(SDK 初始化几秒后才发,且需打开私信抽屉才会拉全)
    _DM_WAIT = ("/v1/message/", "/v1/stranger/", "get_conversation_list",
                "conversation", "/imapi/", "/api/im/web/chat", "/api/im/web/session",
                "/api/im/web/msg")
    # 私信入口候选(图标/抽屉,改版时改这里)
    _DM_ENTRY = ['[data-e2e="im-entry"]', '[data-e2e="message-entry"]',
                 'a[href*="message"]', 'a[href*="/im"]', '[class*="im-entry"]',
                 '[class*="message"]', 'text=私信', 'text=消息']

    page.on("response", on_response)
    # 私信入口可能 window.open 到新标签页(pcim_saas 是独立微应用)。不监听的话:
    # 原 page 的 DOM/URL/XHR 全都不动 —— 看起来就像「点击没生效」,其实是在看错的页。
    popups: List = []

    def on_popup(p):
        if p is page:                     # context 是账号常驻的,别把主页面/别处开的页收进来
            return
        popups.append(p)
        p.on("response", on_response)     # 新页的 XHR 也要拦
        print(f"[dm] douyin popup opened: {p.url!r}")

    page.context.on("page", on_popup)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if "passport" in page.url or "/login" in page.url:
            return [], "logged_out:登录态失效,请重新登录"
        # 上一轮实测:页面没 hydrate 完就点(只有 6 个 XHR、frontier WS 都没连),
        # handler 还没绑上,clickable=True 只是 CSS cursor 的假象。先等网络静默。
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            if platform == "xhs":
                await mgr.xhs_interaction.pause(0.25, 0.55)
            else:
                await page.wait_for_timeout(3000)
        # 标定:枚举页面上所有像「私信/消息」的可点元素(真实 DOM),用来校准入口选择器,
        # 而不是继续盲猜。抖音私信入口是顶栏信封图标(多为 SVG + aria-label,无文字)。
        if platform == "douyin":
            try:
                probe = await page.evaluate(
                    "() => [...document.querySelectorAll('a,button,div,span,li,i')]"
                    ".map(e => ({t:e.tagName, txt:(e.textContent||'').trim().slice(0,10),"
                    " href:e.getAttribute&&e.getAttribute('href'), aria:e.getAttribute&&e.getAttribute('aria-label'),"
                    " cls:(e.className&&e.className.toString?e.className.toString():'').slice(0,40),"
                    " de:e.getAttribute&&e.getAttribute('data-e2e')}))"
                    ".filter(o => /私信|消息|message|\\/im|im-|conversation/i.test("
                    "  [o.txt,o.href,o.aria,o.cls,o.de].join(' ')))"
                    ".slice(0,25)")
                print(f"[dm-probe] douyin entry candidates({len(probe)}): {ascii(probe)}")
            except Exception as e:
                print(f"[dm-probe] douyin probe failed: {ascii(repr(e))}")
        # 抖音:「消息」是 <div>(无 href),React onClick 绑在祖先上,合成 element.click()
        # 不触发。改用真人式坐标点击:定位可点祖先→hover→page.mouse.click,外层容器优先,
        # 每次确认 IM 是否真的 bootstrap(im_hit);再加 JS 点祖先链兜底。
        if platform == "douyin":
            via = ""
            boxes = []
            # 实测:hydrate 完成后抖音会给入口打 data-e2e="im-entry"。优先用它,
            # 比坐标点击稳(坐标只是没有 data-e2e 时的兜底)。
            try:
                entry = page.locator('[data-e2e="im-entry"]').first
                if await entry.is_visible(timeout=2000):
                    await entry.click(timeout=3000)
                    for _ in range(10):
                        await page.wait_for_timeout(500)
                        if im_hit[0]:
                            break
                    if im_hit[0]:
                        via = 'data-e2e=im-entry'
            except Exception:
                pass
            try:
                boxes = await page.evaluate(_DOUYIN_IM_BOXES_JS)
            except Exception:
                boxes = []
            print(f"[dm-probe] douyin message boxes({len(boxes or [])}): {ascii(boxes)}")
            # 嵌套的「消息」DIV 常落在同一坐标,点 3 次和点 1 次等价 —— 去重,省 10s
            seen_pt: Set[tuple] = set()
            ordered = []
            for b in sorted(boxes or [], key=lambda b: (not b["clickable"], b["covered"])):
                pt = (int(b["x"]), int(b["y"]))
                if pt not in seen_pt:
                    seen_pt.add(pt)
                    ordered.append(b)
            before_dom = await _dm_panel_probe(page)
            for b in ordered:             # via 非空时 im_hit 必为真,这里就会跳过
                if im_hit[0]:
                    break
                try:
                    await page.mouse.move(b["x"], b["y"])
                    await page.wait_for_timeout(150)
                    await page.mouse.click(b["x"], b["y"])
                    for _ in range(7):
                        await page.wait_for_timeout(500)
                        if im_hit[0]:
                            break
                    if im_hit[0]:
                        via = f"mouse@{int(b['x'])},{int(b['y'])}({b['tag']})"
                except Exception:
                    continue
            # 点击到底做了什么?没有这个对比,只能反复猜「点没点中」
            after_dom = await _dm_panel_probe(page)
            print(f"[dm-probe] douyin dom before={before_dom}")
            print(f"[dm-probe] douyin dom after ={after_dom}")
            if popups:
                # 私信开在了新标签页:后续等待/滚动/取资料都必须对着它做
                page = popups[-1]
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                via = via or f"popup:{page.url.split('?')[0]}"
                print(f"[dm] douyin switched to popup url={page.url} "
                      f"dom={await _dm_panel_probe(page)}")
            if not im_hit[0]:
                try:
                    await page.evaluate(
                        # 取最内层(.pop())——外层 wrapper 上通常没绑 handler
                        "() => { const hs=[...document.querySelectorAll('div,span,a,button,li')]"
                        ".filter(e => (e.textContent||'').trim()==='消息'); let t=hs.pop();"
                        " for(let d=0;d<5&&t;d++,t=t.parentElement){ try{t.click();}catch(e){} } }")
                    for _ in range(8):
                        await page.wait_for_timeout(500)
                        if im_hit[0]:
                            break
                    if im_hit[0]:
                        via = "js-clickchain"
                except Exception:
                    pass
            douyin_open_via = via
        # 打开私信抽屉/面板(站内图标里,点开才会拉会话列表);小红书私信走 /api/im/web REST。
        # 关键:入口是 flaky 的 —— 「消息」有多个同文本 DIV,.first 常点中非导航的文本节点。
        # 所以不信任单次点击,逐个可见候选点,每点后确认 IM 是否真的 bootstrap(im_hit),命中才停。
        if platform in ("kuaishou", "xhs"):
            clicked_by = ""
            for cand in _DM_ENTRY:
                if im_hit[0] or (platform == "xhs" and clicked_by):
                    break
                try:
                    loc = (page.get_by_text(cand[5:], exact=False)
                           if cand.startswith("text=") else page.locator(cand))
                    n = min(await loc.count(), 5)
                except Exception:
                    continue
                for i in range(n):
                    if im_hit[0]:
                        break
                    try:
                        el = loc.nth(i)
                        if not await el.is_visible():
                            continue
                        if platform == "xhs":
                            await mgr.xhs_interaction.click_visible(el)
                        else:
                            await el.click(timeout=3500)
                        # 确认:等 IM 流量出现(最多 ~3.5s),没起来就试下一个候选
                        for _ in range(7):
                            if platform == "xhs":
                                await mgr.xhs_interaction.pause(0.2, 0.45)
                            else:
                                await page.wait_for_timeout(500)
                            if im_hit[0]:
                                break
                        if im_hit[0] or platform == "xhs":
                            clicked_by = f"{cand}#{i}"
                            break
                    except Exception:
                        continue
            print(f"[dm] {platform} entry clicked_by="
                  f"{clicked_by or 'NONE(IM未起来)'} im_hit={im_hit[0]}")
        # 小红书是 SPA:/im 直开不稳定；只用可见站内入口触发前端路由。
        if platform == "xhs":
            await mgr.xhs_interaction.pause(0.25, 0.55)
        # IM SDK 异步初始化,显式等会话/消息接口回包(最多 ~22s),而不是死等固定时间
        try:
            await page.wait_for_response(
                lambda r: any(h in r.url for h in _DM_WAIT) and r.status == 200,
                timeout=22000)
        except Exception:
            pass
        if platform == "douyin":
            # 打印放在等待之后:IM SDK 是慢启动的,点击那一刻 im_hit 必然还是 False
            print(f"[dm] douyin im open im_hit={im_hit[0]} "
                  f"via={douyin_open_via or 'FAIL'} boxes={len(boxes or [])} "
                  f"url={page.url}")
        if platform == "xhs":
            await mgr.xhs_interaction.pause(0.25, 0.55)
        else:
            await page.wait_for_timeout(settle_ms)
        for _ in range(max_scrolls):
            before = len(collected)
            try:
                if platform == "xhs":
                    await mgr.xhs_interaction.scroll_step(page)
                else:
                    await page.mouse.wheel(0, 2500)
                    await page.evaluate(
                        "() => { let b=null,h=0; document.querySelectorAll('div,ul,section').forEach(e=>{"
                        " const s=getComputedStyle(e); if((s.overflowY==='auto'||s.overflowY==='scroll')"
                        " && e.scrollHeight>e.clientHeight+40 && e.scrollHeight>h){b=e;h=e.scrollHeight;}});"
                        " if(b)b.scrollTop=b.scrollHeight; }")
            except Exception:
                pass
            if platform != "xhs":
                await page.wait_for_timeout(settle_ms)
            if len(collected) == before:
                break
        # wait_for_response 只等到响应头就返回,大包的 body 可能还在下载 —— 不等的话
        # 解析时 dm_init_raw 还是空的,看起来就像「包没来」。
        if platform == "douyin" and im_hit[0] and not dm_init_raw[0]:
            for _ in range(20):           # 最多 ~10s
                await page.wait_for_timeout(500)
                if dm_init_raw[0]:
                    break
            print(f"[dm-init] 等待 body 落地: len={len(dm_init_raw[0])}")
        # 标定用:把会话大包落盘,protobuf 字段号可以离线试,不用每次都跑浏览器
        _dump = os.environ.get("CREATORHUB_DM_DUMP")
        if _dump and dm_init_raw[0]:
            try:
                with open(_dump, "wb") as f:
                    f.write(dm_init_raw[0])
                print(f"[dm-init] 已落盘 {_dump} ({len(dm_init_raw[0])} bytes)")
            except Exception as e:
                print(f"[dm-init] 落盘失败: {e!r}")
        _request_dump = os.environ.get("CREATORHUB_DM_REQUEST_DUMP")
        if _request_dump and dm_init_request_raw[0]:
            try:
                with open(_request_dump, "wb") as f:
                    f.write(dm_init_request_raw[0])
                print(f"[dm-init] request saved {_request_dump} "
                      f"({len(dm_init_request_raw[0])} bytes)")
            except Exception as e:
                print(f"[dm-init] request dump failed: {e!r}")
        # 抖音:会话在 get_message_by_init 的 protobuf 大包里。解会话 → 页面内批量
        # POST im/user/info(按 sec_uid,抖音自己签名)补昵称/头像 → 水合。
        if platform == "douyin" and dm_init_raw[0]:
            try:
                convs_parsed = parse_conversations(dm_init_raw[0])
                # 页面内批量拉资料(sec_uid),补全 im/user/info 自然加载没覆盖到的
                sec_ids = [c["peer_sec_uid"] for c in convs_parsed
                           if c.get("peer_sec_uid")
                           and not im_profiles.get(c["peer_uid"])]
                sec_profiles = await _fetch_im_user_info(page, sec_ids)
                for c in convs_parsed:
                    prof = im_profiles.get(c["peer_uid"]) \
                        or sec_profiles.get(c["peer_sec_uid"]) or {}
                    last_meta = json.dumps({
                        "last_sender_uid": c["last_sender_uid"],
                        "self_uid": c["self_uid"],
                        "last_msg_type": c["last_msg_type"],
                    }, ensure_ascii=False)
                    collected[c["conv_id"]] = {
                        "conv_id": c["conv_id"],
                        "peer_uid": c["peer_uid"],
                        "peer_sec_uid": c["peer_sec_uid"] or prof.get("sec_uid", ""),
                        "peer_nickname": prof.get("nickname", ""),
                        "peer_avatar": prof.get("avatar", ""),
                        "last_text": c["last_text"],
                        "last_time": c.get("last_time") or 0,
                        "unread_count": 0,
                        "conv_short_id": c["conv_short_id"],
                        "ticket": c["ticket"],
                        "raw_json": last_meta,
                    }
                _named = sum(1 for v in collected.values() if v["peer_nickname"])
                print(f"[dm-pb] douyin parsed={len(collected)} "
                      f"im_profiles={len(im_profiles)} sec_profiles={len(sec_profiles)} "
                      f"named={_named}")
                # named 远小于 profiles 总数 = 键对不上,而不是「没拉到资料」。
                # 分别统计三条水合路径的命中数,定位是哪条断了。
                _with_sec = sum(1 for c in convs_parsed if c.get("peer_sec_uid"))
                _by_uid = sum(1 for c in convs_parsed if im_profiles.get(c["peer_uid"]))
                _by_sec = sum(1 for c in convs_parsed
                              if sec_profiles.get(c.get("peer_sec_uid") or "\0"))
                print(f"[dm-pb] convs={len(convs_parsed)} with_sec={_with_sec} "
                      f"hydrated_by_uid={_by_uid} hydrated_by_sec={_by_sec}")
                print(f"[dm-pb] im_profiles keys sample={list(im_profiles)[:3]} "
                      f"peer_uid sample={[c['peer_uid'] for c in convs_parsed[:3]]}")
            except Exception as e:
                print(f"[dm-pb] douyin protobuf parse failed: {e!r}")

        final_url = page.url
    except Exception as e:
        error = f"打开私信页失败: {e!r}"
        final_url = ""
    finally:
        try:
            page.context.remove_listener("page", on_popup)
        except Exception:
            pass
        for p in ([page] + popups):      # popup 也要关,否则残留标签页拖慢下个账号
            try:
                if not p.is_closed():
                    await p.close()
            except Exception:
                pass
    # 标定期:无论成败都打日志(私信接口完全未知,这行最关键)
    print(f"[dm] convs platform={platform} got={len(collected)} im_visible={im_visible} "
          f"hit_urls={hit_urls[:8]} final_url={final_url} api_seen({len(api_seen)})={api_seen[:60]}")
    print(f"[dm-ws] ws_urls({len(ws_urls)})={ws_urls[:10]} frames={ws_frames}")
    for i, smp in enumerate(dm_samples):
        print(f"[dm-sample {i}] {smp}")
    if not collected:
        # 小红书:message/entry 返回 visible=false —— 该账号网页端私信未开放(仅 App),非本项目可解
        if platform == "xhs" and im_visible is False:
            error = ("小红书网页端未开放私信(entry visible=false),仅手机 App 可用;"
                     "无法在网页端同步或收发。")
        else:
            error = error or "未拦截到会话(私信接口未知,需标定/或登录态失效)"
    return list(collected.values()), ("" if collected else error)


async def fetch_dm_history(mgr: BrowserManager, identity, platform: str,
                           conv_id: str, conv_short_id: str, conv_type: int = 1,
                           cursor: int = 0, count: int = 50,
                           debug: bool = False, *, peer_uid: str = "",
                           self_uid: str = "") -> Tuple[dict, str]:
    """无头抓单个会话的历史消息:imapi/v1/message/get_by_conversation(cmd 301)。
    该接口纯 cookie 鉴权、无 a_bogus/签名,用账号常驻 context 的 request 直接 POST。
    返回 (parse_messages 结果, error)。debug=True 时额外打印原始响应 b64(标定用)。"""
    if platform == "rednote":
        return {}, "unavailable:海外小红书暂未开放私信协议通道"
    if platform == "twitter":
        from app.platforms.twitter import TwitterClient
        from app.browser.twitter_fetcher import fetch_twitter_dm_history
        cookie_s = await _twitter_cookie_str(mgr, identity)
        eff_self = self_uid or ""
        if cookie_s:
            client = TwitterClient(
                cookie_s,
                getattr(identity, "ua", "") or getattr(identity, "user_agent", ""),
                proxy=getattr(identity, "proxy", ""),
            )
            eff_self = eff_self or client.self_uid
            async with client.session_scope():
                msgs, err = await client.fetch_dm_messages(conv_id)
                if msgs or err.startswith(("logged_out:", "verification_required:")):
                    return {"messages": msgs, "next_cursor": "", "has_more": False}, err
        msgs, err = await fetch_twitter_dm_history(
            mgr, identity, conv_id, self_uid=eff_self, max_msgs=count
        )
        if err and not msgs:
            return {}, err
        return {"messages": msgs, "next_cursor": "", "has_more": False}, ""
    if platform == "instagram":
        from app.platforms.instagram import InstagramClient
        cookie_s = await _ig_cookie_str(mgr, identity)
        if not cookie_s:
            return {}, "logged_out:缺少 Instagram 登录 Cookie"
        client = InstagramClient(
            cookie_s,
            getattr(identity, "ua", "") or getattr(identity, "user_agent", ""),
            proxy=getattr(identity, "proxy", ""),
            account_seed=getattr(identity, "fp_seed", "") or getattr(identity, "profile_dir", ""),
            timezone_id=getattr(identity, "timezone_id", "Asia/Shanghai"),
        )
        async with client.session_scope():
            msgs, err = await client.fetch_dm_history(
                conv_id, max_pages=max(1, min(5, (count // 20) + 1))
            )
            if err:
                return {}, err
            return {"messages": msgs, "next_cursor": "", "has_more": False}, ""
    if platform == "bilibili":
        from app.platforms.bilibili import BilibiliClient
        cookie_s = await _bili_cookie_str(mgr, identity)
        if not cookie_s:
            return {}, "logged_out:缺少 B 站登录 Cookie"
        client = BilibiliClient(
            cookie_s,
            getattr(identity, "user_agent", ""),
            proxy=getattr(identity, "proxy", ""),
        )
        async with client.session_scope():
            return await client.fetch_dm_messages(
                peer_uid or conv_id,
                size=count,
                end_seqno=int(cursor or 0),
                self_uid=self_uid,
            )
    if platform == "xhs":
        from app.browser.xhs_im import fetch_history as fetch_xhs_history
        return await fetch_xhs_history(
            mgr, identity, conv_id, peer_uid=peer_uid or conv_id,
            self_uid=self_uid, count=count)
    import base64 as _b64
    from app.browser.douyin_im_pb import build_history_request, parse_messages, GET_BY_CONV_URL
    if platform != "douyin":
        return {}, "仅抖音已支持"
    if not conv_id or not conv_short_id:
        return {}, "缺 conv_id / conv_short_id"
    try:
        ctx = await mgr.context_for(identity)      # 常驻无头 context(带 cookie)
        req = build_history_request(conv_id, int(conv_type or 1),
                                    int(conv_short_id), int(cursor or 0), count)
        resp = await ctx.request.post(
            GET_BY_CONV_URL, data=req,
            headers={"content-type": "application/x-protobuf",
                     "referer": "https://www.douyin.com/"})
        body = await resp.body()
        parsed = parse_messages(body)
        if debug:
            print(f"[dm-hist] conv={conv_id} status={resp.status} "
                  f"resp_len={len(body)} msgs={len(parsed.get('messages', []))} "
                  f"next_cursor={parsed.get('next_cursor')}")
            print(f"[dm-hist-raw] b64:{_b64.b64encode(body[:1200]).decode()}")
            # 非文本消息(分享视频=8/图片=27/语音=17...)dump 原始 content JSON,标定字段用
            for _m in parsed.get("messages", []):
                if _m.get("msg_type") not in (7, 0):
                    print(f"[dm-hist-content] type={_m.get('msg_type')} "
                          f"text={_m.get('text')!r} content={_m.get('content')}")
        if resp.status != 200:
            return parsed, f"imapi 返回 {resp.status}"
        return parsed, ""
    except Exception as e:
        return {}, f"抓历史失败: {e!r}"
