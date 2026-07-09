"""抖音 Web 客户端。对应逆向 internal/douyin.NativeClient。
负责:拼接公共参数 + a_bogus 签名 + 携带 Cookie 请求 douyin.com Web API。
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any, Dict, List, Optional, Set

from curl_cffi.requests import AsyncSession

from .signing import sign_url, gen_false_ms_token
from ...netfp import impersonate_for_ua

BASE = "https://www.douyin.com"


def cookie_from_state(storage_state_json: str) -> str:
    """从 Playwright storage_state JSON 提取抖音 Cookie 串(name=value; ...)。
    只取抖音相关域,直连接口(签名 + 该 Cookie)即可请求。"""
    try:
        state = json.loads(storage_state_json or "{}")
    except Exception:
        return ""
    parts = []
    for c in state.get("cookies", []):
        dom = c.get("domain", "")
        if "douyin" in dom or "bytedance" in dom or "amemv" in dom or dom == "":
            name, val = c.get("name"), c.get("value")
            if name and val is not None:
                parts.append(f"{name}={val}")
    return "; ".join(parts)

# douyin Web 公共 query 参数(对应 signing.DefaultRequestParams)
DEFAULT_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "pc_client_type": "1",
    "version_code": "190600",
    "version_name": "19.6.0",
    "cookie_enabled": "true",
    "screen_width": "1536",
    "screen_height": "864",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "130.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "engine_version": "130.0.0.0",
    "os_name": "Windows",
    "os_version": "10",
    "platform": "PC",
    "downlink": "10",
    "effective_type": "4g",
    "round_trip_time": "50",
}


class DouyinClient:
    def __init__(self, cookie: str, user_agent: str, timeout: float = 20.0):
        self.cookie = cookie or ""
        self.ua = user_agent
        self.timeout = timeout
        self.impersonate = impersonate_for_ua(user_agent)  # TLS 指纹复刻,绕 JA3 风控

    def _headers(self, referer: str = BASE + "/") -> Dict[str, str]:
        return {
            "User-Agent": self.ua,
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": self.cookie,
        }

    def _build_url(self, path: str, params: Dict[str, Any]) -> str:
        q = dict(DEFAULT_PARAMS)
        q.update({k: v for k, v in params.items() if v is not None})
        q["msToken"] = gen_false_ms_token()
        qs = urllib.parse.urlencode(q)
        signed = sign_url(qs, self.ua)
        return f"{BASE}{path}?{signed}"

    async def _get_json(self, path: str, params: Dict[str, Any],
                        referer: str = BASE + "/") -> Optional[dict]:
        url = self._build_url(path, params)
        async with AsyncSession() as cli:
            r = await cli.get(url, headers=self._headers(referer),
                              impersonate=self.impersonate, timeout=self.timeout)
            if r.status_code != 200 or not r.content:
                # HTTP 200 + 空 body = 被风控拒了(接口本身可能是活的:follower/list 直连
                # 空 body,页面里发同一个请求却正常)。静默 return None 会让上层把「被拒」
                # 和「没有数据」混为一谈 —— 那正是 raw=0 查了半天的原因。
                print(f"[dy-client] {path} → HTTP {r.status_code} len={len(r.content)}"
                      f"{' (空 body:多半被风控拒,非无数据)' if r.status_code == 200 else ''}")
                return None
            try:
                return r.json()
            except Exception:
                print(f"[dy-client] {path} → 非 JSON len={len(r.content)}")
                return None

    # ── 用户资料(对应 NativeClient.FetchProfile)──
    async def fetch_profile(self, sec_uid: str) -> Optional[dict]:
        data = await self._get_json(
            "/aweme/v1/web/user/profile/other/",
            {"sec_user_id": sec_uid, "publish_video_strategy_type": "2"},
            referer=f"{BASE}/user/{sec_uid}",
        )
        if data and data.get("user"):
            return data["user"]
        return None

    # ── 作品列表(对应 NativeClient.FetchVideoList)──
    async def fetch_video_list(self, sec_uid: str, max_cursor: int = 0,
                               count: int = 20) -> Dict[str, Any]:
        data = await self._get_json(
            "/aweme/v1/web/aweme/post/",
            {
                "sec_user_id": sec_uid,
                "max_cursor": max_cursor,
                "count": count,
                "publish_video_strategy_type": "2",
            },
            referer=f"{BASE}/user/{sec_uid}",
        )
        if not data:
            return {"items": [], "has_more": False, "max_cursor": 0}
        return {
            "items": data.get("aweme_list") or [],
            "has_more": bool(data.get("has_more")),
            "max_cursor": data.get("max_cursor", 0),
        }

    async def fetch_all_new_videos(self, sec_uid: str, known_ids: set,
                                   max_pages: int = 5) -> List[dict]:
        """对应 NativeClient.FetchAllNewVideos:翻页直到遇到已知作品。"""
        new_items: List[dict] = []
        cursor = 0
        for _ in range(max_pages):
            page = await self.fetch_video_list(sec_uid, cursor)
            if not page["items"]:
                break
            stop = False
            for it in page["items"]:
                aweme_id = str(it.get("aweme_id", ""))
                if aweme_id in known_ids:
                    stop = True
                    continue
                new_items.append(it)
            if stop or not page["has_more"]:
                break
            cursor = page["max_cursor"]
        return new_items

    # ── 作品详情(对应 NativeClient.FetchVideoDetail)──
    async def fetch_video_detail(self, aweme_id: str) -> Optional[dict]:
        data = await self._get_json(
            "/aweme/v1/web/aweme/detail/",
            {"aweme_id": aweme_id},
        )
        if data and data.get("aweme_detail"):
            return data["aweme_detail"]
        return None

    # ── 评论(直连 comment/list + reply,分页拉全量;参考 CommentAll)──
    async def fetch_comments_page(self, aweme_id: str, cursor: int = 0,
                                  count: int = 20) -> dict:
        data = await self._get_json(
            "/aweme/v1/web/comment/list/",
            {"aweme_id": aweme_id, "cursor": cursor, "count": count, "item_type": 0},
            referer=f"{BASE}/video/{aweme_id}",
        )
        return data or {}

    async def fetch_replies_page(self, aweme_id: str, comment_id: str, cursor: int = 0,
                                 count: int = 20) -> dict:
        data = await self._get_json(
            "/aweme/v1/web/comment/list/reply/",
            {"item_id": aweme_id, "comment_id": comment_id, "cursor": cursor,
             "count": count, "item_type": 0},
            referer=f"{BASE}/video/{aweme_id}",
        )
        return data or {}

    async def fetch_all_comments(self, aweme_id: str, max_pages: int = 30,
                                 with_replies: bool = True, max_reply_pages: int = 12
                                 ) -> List[dict]:
        """分页拉一条作品的全部一级评论;with_replies 时顺带把有回复的评论的子评论拉全。
        返回原始评论项(含子评论)一维列表,交由上层 parse_comment 归一 + 去重。"""
        out: List[dict] = []
        cursor = 0
        for _ in range(max_pages):
            page = await self.fetch_comments_page(aweme_id, cursor)
            comments = page.get("comments") or []
            out.extend(comments)
            if with_replies:
                for c in comments:
                    if int(c.get("reply_comment_total") or 0) <= 0:
                        continue
                    cid = str(c.get("cid") or "")
                    if not cid:
                        continue
                    rcur = 0
                    for _ in range(max_reply_pages):
                        rp = await self.fetch_replies_page(aweme_id, cid, rcur)
                        out.extend(rp.get("comments") or [])
                        if not rp.get("has_more"):
                            break
                        rcur = rp.get("cursor") or 0
            if not page.get("has_more"):
                break
            cursor = page.get("cursor") or 0
        return out

    # ── 关注 / 粉丝(直连 following/follower list;offset + max_time 分页)──
    #    ⚠️ 参数按抖音 web 常见形态实现;拿不到时上层回退浏览器拦截,故失败无副作用。
    async def _follow_page(self, path: str, user_id: str, sec_uid: str, offset: int,
                           max_time: int, count: int, source_type: int) -> dict:
        data = await self._get_json(
            path,
            {"user_id": user_id, "sec_user_id": sec_uid, "offset": offset,
             "min_time": 0, "max_time": max_time, "count": count,
             "source_type": source_type, "gps_access": 0, "address_book_access": 0},
            referer=f"{BASE}/user/{sec_uid}" if sec_uid else BASE + "/",
        )
        return data or {}

    async def fetch_all_follows(self, user_id: str, sec_uid: str, direction: str,
                                max_pages: int = 25, count: int = 20) -> List[dict]:
        """direction=following(我关注的) / fan(关注我的)。返回原始 user 对象列表。"""
        following = direction == "following"
        path = ("/aweme/v1/web/user/following/list/" if following
                else "/aweme/v1/web/user/follower/list/")
        list_key = "followings" if following else "followers"
        out: List[dict] = []
        seen: Set[str] = set()
        offset = 0
        max_time = 0
        for _ in range(max_pages):
            page = await self._follow_page(path, user_id, sec_uid, offset, max_time,
                                           count, source_type=1)
            users = page.get(list_key) or []
            if not users:
                break
            new = 0
            for u in users:
                uid = str(u.get("uid") or u.get("sec_uid") or "")
                if uid and uid not in seen:
                    seen.add(uid)
                    out.append(u)
                    new += 1
            if not page.get("has_more") or new == 0:
                break
            offset = page.get("offset") or (offset + count)
            max_time = page.get("max_time") or max_time
        return out
