"""多平台本账号已发布作品列表浏览器抓取与直连同步。"""
from __future__ import annotations

import logging
from typing import List, Optional, Set, Tuple

_log = logging.getLogger("creatorhub.browser.hub")

from app.browser.channels_fetcher import fetch_channels_works
from app.browser.fetcher import fetch_videos
from app.browser.hub_common import _bili_cookie_str, _first, _ig_cookie_str, _num, _rednote_cookie_str, _self_profile_link, _twitter_cookie_str
from app.browser.ks_fetcher import fetch_ks_self_profile, fetch_ks_videos
from app.browser.manager import BrowserManager
from app.browser.rednote_fetcher import fetch_rednote_notes
from app.browser.xhs_fetcher import fetch_xhs_notes
from app.platforms.kuaishou import parse_self_user as parse_ks_self_user


# ─────────── 原始作品项 → AccountWork 扁平 dict ───────────
def _norm_douyin_work(it: dict) -> Optional[dict]:
    aid = str(it.get("aweme_id") or "")
    if not aid:
        return None
    stats = it.get("statistics") or {}
    cover = ((it.get("video") or {}).get("cover") or {}).get("url_list") or []
    return {
        "item_id": aid,
        "desc": (it.get("desc") or "").strip(),
        "media_type": "images" if it.get("images") else "video",
        "cover_url": cover[0] if cover else "",
        "create_time": int(it.get("create_time") or 0),
        "like_count": _num(stats.get("digg_count")),
        "comment_count": _num(stats.get("comment_count")),
        "collect_count": _num(stats.get("collect_count")),
        "share_count": _num(stats.get("share_count")),
        "play_count": _num(stats.get("play_count")),
        "status": "",
    }


def _norm_xhs_work(it: dict) -> Optional[dict]:
    card = it.get("note_card") or it
    nid = str(_first(it, "note_id", "id", default="")
              or _first(card, "note_id", "id", default=""))
    if not nid:
        return None
    cover = card.get("cover") or {}
    cov = _first(cover, "url_default", "url_pre", "url", default="")
    if not cov and isinstance(cover.get("info_list"), list) and cover["info_list"]:
        cov = cover["info_list"][0].get("url", "")
    inter = card.get("interact_info") or {}
    return {
        "item_id": nid,
        "desc": _first(card, "display_title", "title", "desc", default=""),
        "media_type": "video" if card.get("type") == "video" else "images",
        "cover_url": cov,
        "create_time": int(_num(card.get("time")) / 1000) if _num(card.get("time")) > 1e12
                       else _num(card.get("time")),
        "like_count": _num(inter.get("liked_count")),
        "comment_count": _num(inter.get("comment_count")),
        "collect_count": _num(inter.get("collected_count")),
        "share_count": _num(inter.get("share_count")),
        "play_count": 0,
        "status": "",
        # 抓评论/打开笔记要用;SSR 项在 item 顶层,拦截项也可能带
        "xsec_token": str(_first(it, "xsec_token", "xsecToken", default="")
                          or _first(card, "xsec_token", "xsecToken", default="")),
    }


def _norm_ks_work(feed: dict) -> Optional[dict]:
    photo = (feed.get("photo") or {}) if isinstance(feed, dict) else {}
    pid = str(_first(photo, "id", default="") or feed.get("id") or "")
    if not pid:
        return None
    ts = _num(photo.get("timestamp"))
    return {
        "item_id": pid,
        "desc": (_first(photo, "caption", "name", default="") or "").strip(),
        "media_type": "images" if photo.get("atlas") or photo.get("imgUrls") else "video",
        "cover_url": _first(photo, "coverUrl", "cover_url", "webpCoverUrl", default=""),
        "create_time": int(ts / 1000) if ts > 1e12 else ts,
        "like_count": _num(_first(photo, "realLikeCount", "likeCount", default=0)),
        "comment_count": _num(photo.get("commentCount")),
        "collect_count": 0,
        "share_count": _num(photo.get("shareCount")),
        "play_count": _num(_first(photo, "viewCount", "playCount", default=0)),
        "status": "",
    }


def _norm_channels_work(it: dict) -> Optional[dict]:
    """视频号助手 post_list 一项 -> 本账号作品 dict。视频号视频加密不可下载,
    这里只记元数据+统计(供本账号作品展示 + 作品健康监控)。字段以真机抓包为准。"""
    if not isinstance(it, dict):
        return None
    oid = ""
    for _id_key in ("objectId", "exportId", "id"):
        _cand = str(it.get(_id_key) or "").strip()
        if _cand and _cand != "0":
            oid = _cand
            break
    if not oid:
        return None
    # 助手当前 post_list 使用 desc；旧版/部分接口使用 objectDesc。
    od = _first(it, "objectDesc", "desc", default={}) or {}
    if not isinstance(od, dict):
        od = {}
    media = (od.get("media") or it.get("media") or [])
    m0 = media[0] if media and isinstance(media[0], dict) else {}
    fmt = str(_first(m0, "fileFormat", "mediaType", default="")).lower()
    media_type_code = _first(od, "mediaType", default=None)
    ts = _num(_first(it, "createtime", "createTime", default=0))
    description = (_first(od, "description", "shortTitle", default="")
                   or _first(it, "title", "description", default="") or "")
    if not isinstance(description, str):
        description = str(description)
    return {
        "item_id": oid,
        "desc": description.strip(),
        "media_type": ("images"
                       if media_type_code == 2 or fmt == "2"
                       or "pic" in fmt or "image" in fmt
                       else "video"),
        "cover_url": _first(m0, "coverUrl", "thumbUrl", default="")
                     or _first(it, "coverUrl", default=""),
        "create_time": int(ts / 1000) if ts > 1e12 else ts,
        "like_count": _num(_first(it, "likeCount", "like_count", default=0)),
        "comment_count": _num(_first(it, "commentCount", "comment_count", default=0)),
        "collect_count": _num(_first(it, "favCount", "collectCount", default=0)),
        "share_count": _num(_first(it, "forwardCount", "shareCount", default=0)),
        "play_count": _num(_first(it, "readCount", "playCount", "viewCount", default=0)),
        "status": str(_first(it, "statusText", "auditStatus", default="") or ""),
    }


def _norm_tiktok_work(it: dict) -> Optional[dict]:
    from app.platforms.tiktok import parse_tiktok_aweme
    aweme = parse_tiktok_aweme(it)
    if aweme is None:
        return None
    return {
        "item_id": aweme.aweme_id,
        "desc": aweme.desc,
        "media_type": aweme.media_type,
        "cover_url": aweme.cover,
        "create_time": aweme.create_time,
        "like_count": aweme.like_count,
        "comment_count": aweme.comment_count,
        "collect_count": 0,
        "share_count": 0,
        "play_count": 0,
        "status": "",
    }


def _norm_bili_work(it: dict) -> Optional[dict]:
    from app.platforms.bilibili import parse_bili_aweme, parse_bili_creator_work
    if isinstance(it, dict) and ("Archive" in it or "state_desc" in it):
        return parse_bili_creator_work(it)
    aweme = parse_bili_aweme(it)
    if aweme is None:
        return None
    stat = it.get("stat") if isinstance(it.get("stat"), dict) else {}
    return {
        "item_id": aweme.aweme_id,
        "desc": aweme.desc.splitlines()[0] if aweme.desc else "",
        "media_type": aweme.media_type,
        "cover_url": aweme.cover or "",
        "create_time": aweme.create_time,
        "like_count": aweme.like_count,
        "comment_count": aweme.comment_count,
        "collect_count": _num(_first(stat, "favorite", "fav", default=0) or _first(it, "favorites", default=0)),
        "share_count": _num(_first(stat, "share", default=0)),
        "play_count": _num(_first(stat, "view", default=0) or _first(it, "play", default=0)),
        "status": "",
    }


def _norm_rednote_work(it: dict) -> Optional[dict]:
    """海外小红书个人主页笔记或创作者中心已发布笔记 -> AccountWork dict。"""
    base = _norm_xhs_work(it)
    if not base:
        return None
    card = it.get("note_card") or it
    inter = card.get("interact_info") or {}
    base["like_count"] = base["like_count"] or _num(
        _first(card, "likes", "liked_count", default=0)
    )
    base["comment_count"] = base["comment_count"] or _num(
        _first(card, "comments_count", "comment_count", default=0)
    )
    base["collect_count"] = base["collect_count"] or _num(
        _first(card, "collected_count", "collect_count", default=0)
    )
    base["share_count"] = base["share_count"] or _num(
        _first(card, "shared_count", "share_count", default=0)
    )
    base["play_count"] = _num(
        _first(card, "view_count", "play_count", default=0)
        or _first(inter, "view_count", default=0)
    )
    perm_msg = str(_first(card, "permission_msg", "status_desc", default="") or "").strip()
    perm_code = card.get("permission_code")
    if not perm_msg and perm_code is not None:
        try:
            perm_msg = "仅自己可见" if int(perm_code) == 1 else ""
        except (TypeError, ValueError):
            perm_msg = ""
    base["status"] = perm_msg
    return base


def _norm_ig_work(it: dict) -> Optional[dict]:
    from app.platforms.instagram import parse_ig_creator_work
    return parse_ig_creator_work(it)


def _norm_twitter_work(it: dict) -> Optional[dict]:
    from app.platforms.twitter import parse_twitter_creator_work
    return parse_twitter_creator_work(it)


async def fetch_account_works(mgr: BrowserManager, identity, platform: str, uid: str,
                              max_scrolls: int = 14) -> Tuple[List[dict], str]:
    """抓取登录账号自己发布的作品(复用各平台已有的主页拦截抓取)。
    返回 (归一后的作品 dict 列表, error)。需要账号已知自身 uid/sec_uid。
    入参用 identity/platform/uid 原语(由调用方在 session 活跃时取出),避免 ORM 实例失效。"""
    uid = (uid or "").strip()
    open_url = ""
    if platform == "xhs":
        # 小红书:站内「我」入口拿真实主页链接(带 xsec_token);失败再退回 uid 直开
        open_url, self_uid = await _self_profile_link(mgr, identity, "xhs")
        uid = uid or self_uid
    elif platform == "kuaishou" and (not uid or uid.isdigit()):
        # 快手:header 抓链接不稳(实测 href='');/profile 只认真实 3x id(纯数字 userId 会 404)。
        # 改用可靠法 —— cookie 数字 userId 反查本人 3x id(与「刷新资料」同一套 fetch_ks_self_profile)
        try:
            prof, perr = await fetch_ks_self_profile(mgr, identity)
            if perr == "logged_out":
                return [], "logged_out:登录态失效,请重新登录"
            self3x = str(parse_ks_self_user(prof or {}).get("sec_uid") or "")
            if self3x:
                uid = self3x
        except Exception as e:
            _log.warning("快手本人主页 ID 反查失败: %r", e)
    # 视频号 / B站 / 海外小红书创作中心 / Instagram / Twitter: 可先按本账号 Cookie 读取自身资料与作品
    if not uid and not open_url and platform not in ("shipinhao", "bilibili", "rednote", "instagram", "twitter"):
        return [], "missing_uid:账号缺自身 uid,请先点账号「刷新资料」再同步作品"

    known: Set[str] = set()
    try:
        if platform == "shipinhao":
            items, _author, err = await fetch_channels_works(
                mgr,
                identity,
                known,
                max_scrolls=max_scrolls,
                max_pages=max(120, max_scrolls * 8),
            )
            norm = _norm_channels_work
        elif platform == "rednote":
            from app.platforms.rednote import RednoteApiClient, RednoteApiError, has_a1
            cookie_s = await _rednote_cookie_str(mgr, identity)
            if cookie_s and has_a1(cookie_s):
                client = RednoteApiClient(
                    cookie_s,
                    mgr.direct_request_user_agent(identity)
                    if hasattr(mgr, "direct_request_user_agent")
                    else (getattr(identity, "ua", "") or getattr(identity, "user_agent", "")),
                    proxy=getattr(identity, "proxy", ""),
                )
                try:
                    async with client.session_scope():
                        try:
                            pub_items = await client.published_notes(max_pages=5)
                            if pub_items:
                                out_rn = [w for w in (_norm_rednote_work(it) for it in pub_items) if w]
                                if out_rn:
                                    return out_rn, ""
                        except RednoteApiError:
                            pass
                        if not uid:
                            me = await client.self_info()
                            uid = str(me.get("user_id") or me.get("userId") or "").strip()
                        if uid:
                            posted_res = await client.notes_by_creator(
                                uid, page_size=min(30, max(20, max_scrolls * 10))
                            )
                            api_notes = posted_res.get("notes") or []
                            out_rn = [w for w in (_norm_rednote_work(it) for it in api_notes) if w]
                            if out_rn:
                                return out_rn, ""
                except Exception:
                    pass
            open_url, self_uid = await _self_profile_link(mgr, identity, "rednote")
            uid = uid or self_uid
            if not uid and not open_url:
                return [], "missing_uid:账号缺自身 uid,请先点账号「刷新资料」再同步作品"
            items, _author, err = await fetch_rednote_notes(
                mgr,
                identity,
                uid,
                known,
                xsec_token="",
                max_scrolls=max_scrolls,
                open_url=open_url,
                ssr_fallback=True,
            )
            norm = _norm_rednote_work
        elif platform == "bilibili":
            from app.platforms.bilibili import BilibiliClient
            from app.browser.bili_fetcher import fetch_bili_videos
            cookie_s = await _bili_cookie_str(mgr, identity)
            if cookie_s:
                client = BilibiliClient(
                    cookie_s,
                    getattr(identity, "ua", "") or getattr(identity, "user_agent", ""),
                    proxy=getattr(identity, "proxy", ""),
                    account_seed=getattr(identity, "fp_seed", ""),
                )
                async with client.session_scope():
                    works, cerr = await client.fetch_all_creator_archives(
                        max_pages=max(1, min(8, max_scrolls // 2 or 3))
                    )
                if works or cerr.startswith("logged_out") or cerr == "":
                    return works, ("" if works else (cerr or "empty"))
            if not uid:
                return [], "missing_uid:账号缺自身 uid,请先点账号「刷新资料」再同步作品"
            items, _author, err = await fetch_bili_videos(
                mgr, identity, uid, known, max_scrolls=max_scrolls
            )
            norm = _norm_bili_work
        elif platform == "instagram":
            from app.platforms.instagram import InstagramClient
            from app.browser.ig_fetcher import fetch_ig_posts
            cookie_s = await _ig_cookie_str(mgr, identity)
            target_ref = uid
            if cookie_s:
                client = InstagramClient(
                    cookie_s,
                    getattr(identity, "ua", "") or getattr(identity, "user_agent", ""),
                    proxy=getattr(identity, "proxy", ""),
                    account_seed=getattr(identity, "fp_seed", "") or getattr(identity, "profile_dir", ""),
                    timezone_id=getattr(identity, "timezone_id", "Asia/Shanghai"),
                )
                async with client.session_scope():
                    if not target_ref:
                        prof, perr = await client.fetch_self_profile()
                        if perr.startswith(("logged_out:", "verification_required:")):
                            return [], perr
                        target_ref = str(prof.get("uid") or prof.get("unique_id") or client.self_uid)
                    if target_ref:
                        works, cerr = await client.fetch_all_creator_works(
                            target_ref,
                            max_pages=max(1, min(8, max_scrolls // 2 or 3)),
                        )
                        if works or cerr.startswith(("logged_out:", "verification_required:")):
                            return works, ("" if works else cerr)
            if not target_ref:
                return [], "missing_uid:账号缺自身用户名或 uid,请先点账号「刷新资料」再同步作品"
            items, _author, err = await fetch_ig_posts(
                mgr, identity, target_ref, known, max_scrolls=max_scrolls
            )
            norm = _norm_ig_work
        elif platform == "twitter":
            from app.platforms.twitter import TwitterClient, parse_twitter_creator_work
            from app.browser.twitter_fetcher import fetch_twitter_self_profile, fetch_twitter_user_tweets
            cookie_s = await _twitter_cookie_str(mgr, identity)
            target_ref = uid
            if cookie_s:
                client = TwitterClient(
                    cookie_s,
                    getattr(identity, "ua", "") or getattr(identity, "user_agent", ""),
                    proxy=getattr(identity, "proxy", ""),
                )
                async with client.session_scope():
                    if not target_ref:
                        prof, perr = await client.fetch_self_profile()
                        if perr.startswith(("logged_out:", "verification_required:")):
                            return [], perr
                        target_ref = str(prof.get("unique_id") or prof.get("sec_uid") or prof.get("uid") or client.self_uid)
                    if target_ref:
                        raw_tws, _uinfo, cerr = await client.fetch_user_tweets(
                            target_ref,
                            max_pages=max(1, min(8, max_scrolls // 2 or 3)),
                        )
                        if raw_tws:
                            works = [w for w in (parse_twitter_creator_work(x) for x in raw_tws) if w]
                            if works:
                                return works, ""
                        if cerr.startswith(("logged_out:", "verification_required:")):
                            return [], cerr
            if not target_ref:
                prof, perr = await fetch_twitter_self_profile(mgr, identity)
                if perr.startswith(("logged_out:", "verification_required:")):
                    return [], perr
                target_ref = str(prof.get("unique_id") or prof.get("sec_uid") or prof.get("uid") or "")
            if not target_ref:
                return [], "missing_uid:账号缺自身 @username 或 uid,请先点账号「刷新资料」再同步作品"
            items, _author, err = await fetch_twitter_user_tweets(
                mgr, identity, target_ref, known, max_scrolls=max_scrolls
            )
            norm = _norm_twitter_work
        elif platform == "tiktok":
            from app.browser.tiktok_fetcher import fetch_tiktok_profile_works
            items, err = await fetch_tiktok_profile_works(
                mgr, identity, uid, max_scrolls=max_scrolls)
            norm = _norm_tiktok_work
        elif platform == "xhs":
            items, _author, err = await fetch_xhs_notes(mgr, identity, uid, known,
                                                        xsec_token="",
                                                        max_scrolls=max_scrolls,
                                                        open_url=open_url,
                                                        ssr_fallback=True)
            norm = _norm_xhs_work
        elif platform == "kuaishou":
            items, _author, err = await fetch_ks_videos(mgr, identity, uid, known,
                                                        max_scrolls=max_scrolls,
                                                        open_url=open_url)
            norm = _norm_ks_work
        else:
            items, _author, err = await fetch_videos(mgr, identity, uid, known,
                                                     max_scrolls=max_scrolls)
            norm = _norm_douyin_work
    except Exception as e:
        return [], f"抓取作品异常: {e!r}"

    out = [w for w in (norm(it) for it in (items or [])) if w]
    return out, ("" if out else err)


async def fetch_douyin_account_works_api(cookie: str, user_agent: str, uid: str,
                                         timeout: float = 20.0,
                                         proxy: str = "", *,
                                         environment: dict | None = None
                                         ) -> Tuple[List[dict], str]:
    """读取抖音本账号作品 Web API，并归一化为 AccountWork 字段。"""
    from app.platforms.douyin import DouyinClient
    uid = (uid or "").strip()
    if not uid:
        return [], "missing_uid:账号缺自身 uid"
    if not cookie:
        return [], "no_cookie"
    client = DouyinClient(
        cookie, user_agent, timeout=timeout, proxy=proxy,
        **(environment or {}))
    try:
        async with client.session_scope():
            raw = await client.fetch_all_video_list(uid)
    except Exception as exc:
        return [], f"api:{type(exc).__name__}"
    out = [w for w in (_norm_douyin_work(it) for it in raw) if w]
    return out, ("" if out else (client.last_error or "empty"))
