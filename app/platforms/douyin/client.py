"""抖音 Web 客户端。对应逆向 internal/douyin.NativeClient。
负责:拼接公共参数 + a_bogus 签名 + 携带 Cookie 请求 douyin.com Web API。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Set

from curl_cffi.requests import AsyncSession

import re

from .signing import sign_url, gen_false_ms_token, gen_real_ms_token
from ...netfp import impersonate_for_ua

BASE = "https://www.douyin.com"
IMAPI_SEND_URL = "https://imapi.douyin.com/v1/message/send"
IMAPI_CREATE_URL = "https://imapi.douyin.com/v2/conversation/create"


def cookie_from_state(storage_state_json: str) -> str:
    """从 Patchright storage_state JSON 提取抖音 Cookie 串(name=value; ...)。
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
    def __init__(self, cookie: str, user_agent: str, timeout: float = 20.0,
                 proxy: str = "", *, locale: str = "zh-CN",
                 accept_language: str = "", screen_width: int = 1536,
                 screen_height: int = 864):
        self.cookie = cookie or ""
        self.ua = user_agent
        self.timeout = timeout
        self.proxy = (proxy or "").strip()
        self.locale = str(locale or "zh-CN")
        self.accept_language = (str(accept_language or "").strip()
                                or f"{self.locale},{self.locale.split('-', 1)[0]};q=0.9")
        self.screen_width = max(320, int(screen_width or 1536))
        self.screen_height = max(240, int(screen_height or 864))
        self.impersonate = impersonate_for_ua(user_agent)  # TLS 指纹复刻,绕 JA3 风控
        self._session: AsyncSession | None = None
        self._ms_token: str | None = None
        self._im_sequence_id = 10000
        self.last_error: str = ""
        # 只有没有拿到可判定的响应时才置 True；上层据此把任务标记为
        # uncertain，绝不以浏览器重试同一条写请求造成重复评论/私信/关注。
        self.last_write_uncertain: bool = False

    @asynccontextmanager
    async def session_scope(self):
        """在一个读取任务内复用 curl 会话、Cookie jar 与 msToken。"""
        if self._session is not None:
            yield self
            return
        self._session = AsyncSession(impersonate=self.impersonate)
        try:
            yield self
        finally:
            session, self._session = self._session, None
            if session is not None:
                await session.close()
            self._ms_token = None

    def _headers(self, referer: str = BASE + "/") -> Dict[str, str]:
        return {
            "User-Agent": self.ua,
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": self.accept_language,
            "Cookie": self.cookie,
        }

    def _im_request_options(self, referer: str = BASE + "/") -> Dict[str, Any]:
        self._im_sequence_id += 1
        return {
            "sequence_id": self._im_sequence_id,
            "user_agent": self.ua,
            "locale": self.locale,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "referer": referer,
        }

    def _build_url(self, path: str, params: Dict[str, Any]) -> str:
        q = dict(DEFAULT_PARAMS)
        # Keep direct requests aligned with the account's persistent browser
        # identity.  Cookie, UA/TLS, locale, viewport and proxy all come from
        # the same account row; no mutable session is shared across accounts.
        q.update({
            "screen_width": str(self.screen_width),
            "screen_height": str(self.screen_height),
            "browser_language": self.locale,
        })
        # 公共参数里的 browser/engine 版本对齐本客户端 UA 的 Chrome 大版本,
        # 否则 UA 头说一个版本、query 里 browser_version 又是写死的 130,自相矛盾易被风控识别。
        m = re.search(r"Chrome/(\d+)", self.ua or "")
        if m:
            ver = f"{m.group(1)}.0.0.0"
            q["browser_version"] = ver
            q["engine_version"] = ver
        q.update({k: v for k, v in params.items() if v is not None})
        q["msToken"] = self._ms_token or gen_false_ms_token()
        qs = urllib.parse.urlencode(q)
        signed = sign_url(qs, self.ua)
        return f"{BASE}{path}?{signed}"

    async def _get_json(self, path: str, params: Dict[str, Any],
                        referer: str = BASE + "/") -> Optional[dict]:
        self.last_error = ""
        url = self._build_url(path, params)
        owned = self._session is None
        cli = self._session or AsyncSession(impersonate=self.impersonate)
        try:
            if self._ms_token is None:
                # 获取真实 token 失败时 gen_real_ms_token 自身回退随机 token。
                # 只在会话第一次请求前执行，避免每页重复访问 mssdk。
                self._ms_token = await gen_real_ms_token(
                    self.ua, timeout=min(self.timeout, 10.0),
                    cookie=self.cookie, proxy=self.proxy)
                url = self._build_url(path, params)
            r = await cli.get(url, headers=self._headers(referer),
                              impersonate=self.impersonate, timeout=self.timeout,
                              proxy=self.proxy or None)
            if r.status_code != 200 or not r.content:
                # HTTP 200 + 空 body = 被风控拒了(接口本身可能是活的:follower/list 直连
                # 空 body,页面里发同一个请求却正常)。静默 return None 会让上层把「被拒」
                # 和「没有数据」混为一谈 —— 那正是 raw=0 查了半天的原因。
                self.last_error = f"http_{r.status_code}" if r.status_code != 200 else "empty_body"
                print(f"[dy-client] {path} → HTTP {r.status_code} len={len(r.content)}"
                      f"{' (空 body:多半被风控拒,非无数据)' if r.status_code == 200 else ''}")
                return None
            try:
                return r.json()
            except Exception:
                self.last_error = "invalid_json"
                print(f"[dy-client] {path} → 非 JSON len={len(r.content)}")
                return None
        except Exception as exc:
            self.last_error = f"network:{type(exc).__name__}"
            raise
        finally:
            if owned:
                await cli.close()
                self._ms_token = None

    async def _post_json(self, path: str, params: Dict[str, Any],
                         form: Dict[str, Any], *,
                         referer: str = BASE + "/") -> Optional[dict]:
        """发送签名的网页表单 POST，且为非幂等请求保留不确定状态。

        接口明确返回非 2xx 或业务 ``status_code`` 时调用方可选择页面回退；
        超时、连接中断、5xx、200 空响应均无法确认服务端是否已经落库，调用方
        必须停止重试并把任务标为 ``uncertain``。
        """
        self.last_error = ""
        self.last_write_uncertain = False
        owned = self._session is None
        cli = self._session or AsyncSession(impersonate=self.impersonate)
        try:
            if self._ms_token is None:
                self._ms_token = await gen_real_ms_token(
                    self.ua, timeout=min(self.timeout, 10.0),
                    cookie=self.cookie, proxy=self.proxy)
            url = self._build_url(path, params)
            headers = self._headers(referer)
            headers.update({
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Origin": BASE,
            })
            response = await cli.post(
                url, data={k: str(v) for k, v in form.items() if v is not None},
                headers=headers, impersonate=self.impersonate, timeout=self.timeout,
                proxy=self.proxy or None)
            if response.status_code != 200 or not response.content:
                self.last_error = (f"http_{response.status_code}"
                                   if response.status_code != 200 else "empty_body")
                self.last_write_uncertain = (
                    response.status_code >= 500 or response.status_code == 200)
                return None
            try:
                return response.json()
            except Exception:
                self.last_error = "invalid_json"
                self.last_write_uncertain = True
                return None
        except Exception as exc:
            self.last_error = f"network:{type(exc).__name__}"
            self.last_write_uncertain = True
            return None
        finally:
            if owned:
                await cli.close()
                self._ms_token = None

    def _write_result(self, data: Optional[dict], fallback_error: str) -> tuple[bool, str]:
        """归一网页写接口的业务状态；返回 ``(success, error)``。"""
        if not isinstance(data, dict):
            return False, fallback_error
        code = data.get("status_code")
        if code is None:
            self.last_error = self.last_error or "invalid_response"
            self.last_write_uncertain = True
            return False, self._write_error(fallback_error)
        if str(code) == "0":
            return True, ""
        message = str(data.get("status_msg") or data.get("message") or "")
        return False, f"api_rejected:status_code={code}{(' ' + message) if message else ''}"

    def _write_error(self, fallback: str) -> str:
        error = self.last_error or fallback
        return f"write_uncertain:{error}" if self.last_write_uncertain else error

    @staticmethod
    def _im_rejection(parsed: dict) -> str:
        """Normalize an IM business envelope without hiding auth expiry."""
        message = str(parsed.get("msg") or "")
        error_code = int(parsed.get("error_code") or 0)
        if error_code == 1 and "session length" in message.casefold():
            return f"logged_out:imapi {message}"
        return (f"api_rejected:code={error_code}"
                f"{(' ' + message) if message else ''}")

    # ── 写操作(网页接口,需显式 douyin_write_mode=api|hybrid) ──
    async def post_comment(self, aweme_id: str, text: str, *,
                           reply_comment_id: str = "") -> tuple[bool, str, str]:
        """直连发表一级评论或回复，返回 ``(ok, comment_id, error)``。"""
        aweme_id, text = str(aweme_id or ""), str(text or "").strip()
        if not aweme_id:
            return False, "", "缺少 aweme_id"
        if not text:
            return False, "", "空评论"
        reply_comment_id = str(reply_comment_id or "")
        data = await self._post_json(
            "/aweme/v1/web/comment/publish/",
            {"aweme_id": aweme_id, "item_type": 0},
            {
                "aweme_id": aweme_id,
                "text": text,
                "item_type": 0,
                # 一级评论保持空值；回复同时带两种页面版本使用的关联字段。
                "reply_id": reply_comment_id,
                "reply_comment_id": reply_comment_id,
                "at_users": "[]",
            },
            referer=f"{BASE}/video/{aweme_id}")
        ok, error = self._write_result(data, self._write_error("comment_no_response"))
        if not ok:
            return False, "", error
        comment = data.get("comment") or {}
        cid = str(comment.get("cid") or comment.get("comment_id")
                  or data.get("comment_id") or "ok")
        return True, cid, ""

    async def set_follow_state(self, user_id: str, sec_uid: str = "", *,
                               unfollow: bool = False) -> tuple[bool, str]:
        """直连关注/取关网页接口；缺 user_id 时先以 sec_uid 解析资料。"""
        user_id, sec_uid = str(user_id or ""), str(sec_uid or "")
        if not user_id and sec_uid:
            profile = await self.fetch_profile(sec_uid)
            user_id = str((profile or {}).get("uid")
                          or (profile or {}).get("user_id") or "")
        if not user_id:
            return False, "缺少 target_uid，且无法由 sec_uid 解析用户"
        data = await self._post_json(
            "/aweme/v1/web/commit/follow/user/",
            {"user_id": user_id, "sec_user_id": sec_uid},
            {
                "user_id": user_id,
                "sec_user_id": sec_uid,
                "type": 2 if unfollow else 1,
                "from": 0,
            },
            referer=f"{BASE}/user/{sec_uid or user_id}")
        return self._write_result(
            data, self._write_error("follow_no_response"))

    async def create_dm_conversation(
            self, target_uid: str, self_uid: str, *, target_sec_uid: str = ""
    ) -> tuple[Optional[dict], str]:
        """通过网页端 cmd=609 创建单聊，返回 cmd=100 所需会话标识。"""
        from ...browser.douyin_im_pb import (
            build_create_conversation_request,
            parse_create_conversation_response,
        )

        target_uid = str(target_uid or "").strip()
        self_uid = str(self_uid or "").strip()
        if not target_uid.isdigit() or not self_uid.isdigit():
            return None, "建会需要目标和当前账号的数字 uid"
        if target_uid == self_uid:
            return None, "不能给当前账号创建单聊"
        self.last_error = ""
        self.last_write_uncertain = False
        owned = self._session is None
        cli = self._session or AsyncSession(impersonate=self.impersonate)
        referer = f"{BASE}/user/{target_sec_uid or target_uid}"
        try:
            body = build_create_conversation_request(
                target_uid, self_uid,
                **self._im_request_options(referer))
            response = await cli.post(
                IMAPI_CREATE_URL, data=body,
                headers={**self._headers(referer),
                         "Accept": "application/x-protobuf",
                         "Content-Type": "application/x-protobuf",
                         "Origin": BASE},
                impersonate=self.impersonate, timeout=self.timeout,
                proxy=self.proxy or None)
            raw = response.content
            if response.status_code != 200 or not raw:
                self.last_error = (f"http_{response.status_code}"
                                   if response.status_code != 200 else "empty_body")
                self.last_write_uncertain = (
                    response.status_code >= 500 or response.status_code == 200)
                return None, self._write_error("imapi_create_no_response")
            parsed = parse_create_conversation_response(raw)
            if parsed.get("ok"):
                return dict(parsed["conversation"]), ""
            message = str(parsed.get("msg") or "")
            error_code = int(parsed.get("error_code") or 0)
            if not error_code and message in {"", "OK"}:
                self.last_error = "invalid_response"
                self.last_write_uncertain = True
                return None, self._write_error("imapi_create_invalid_response")
            return None, self._im_rejection(parsed)
        except Exception as exc:
            self.last_error = f"network:{type(exc).__name__}"
            self.last_write_uncertain = True
            return None, self._write_error("imapi_create_network")
        finally:
            if owned:
                await cli.close()
                self._ms_token = None

    async def send_dm(self, conv_id: str, conv_short_id: str, ticket: str,
                      text: str, *, conv_type: int = 1) -> tuple[bool, str]:
        """直连 imapi protobuf 发送已有会话私信，不创建或猜测会话。"""
        from ...browser.douyin_im_pb import build_send_request, parse_send_response

        text = str(text or "").strip()
        if not text:
            return False, "空内容"
        if not (conv_id and conv_short_id and ticket):
            return False, "缺 conv_id/short_id/ticket(先同步会话列表)"
        self.last_error = ""
        self.last_write_uncertain = False
        owned = self._session is None
        cli = self._session or AsyncSession(impersonate=self.impersonate)
        try:
            body = build_send_request(
                str(conv_id), int(conv_type or 1), int(conv_short_id), str(ticket),
                text, str(uuid.uuid4()), int(time.time() * 1000),
                **self._im_request_options())
            response = await cli.post(
                IMAPI_SEND_URL, data=body,
                headers={**self._headers(),
                         "Accept": "application/x-protobuf",
                         "Content-Type": "application/x-protobuf",
                         "Origin": BASE},
                impersonate=self.impersonate, timeout=self.timeout,
                proxy=self.proxy or None)
            raw = response.content
            if response.status_code != 200 or not raw:
                self.last_error = (f"http_{response.status_code}"
                                   if response.status_code != 200 else "empty_body")
                self.last_write_uncertain = (
                    response.status_code >= 500 or response.status_code == 200)
                return False, self._write_error("imapi_no_response")
            parsed = parse_send_response(raw)
            if parsed.get("ok") and parsed.get("cmd") == 100:
                return True, ""
            if (parsed.get("ok") or
                    (not parsed.get("msg") and not parsed.get("error_code"))):
                self.last_error = "invalid_response"
                self.last_write_uncertain = True
                return False, self._write_error("imapi_invalid_response")
            return False, self._im_rejection(parsed)
        except Exception as exc:
            self.last_error = f"network:{type(exc).__name__}"
            self.last_write_uncertain = True
            return False, self._write_error("imapi_network")
        finally:
            if owned:
                await cli.close()
                self._ms_token = None

    @staticmethod
    def _profile_avatar(profile: dict) -> str:
        for key in ("avatar_thumb", "avatar_small", "avatar_larger", "avatar"):
            value = profile.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
            if isinstance(value, dict):
                urls = value.get("url_list") or value.get("urlList") or []
                if isinstance(urls, list) and urls:
                    return str(urls[0])
                for nested in ("url", "url_default"):
                    if isinstance(value.get(nested), str):
                        return value[nested]
        return ""

    async def fetch_im_user_profiles(self, sec_uids: List[str]) -> Dict[str, dict]:
        """用签名网页接口批量补齐 IM protobuf 中缺失的昵称和头像。"""
        unique = list(dict.fromkeys(str(value) for value in sec_uids if value))
        profiles: Dict[str, dict] = {}
        for offset in range(0, len(unique), 20):
            chunk = unique[offset:offset + 20]
            data = await self._post_json(
                "/aweme/v1/web/im/user/info/", {},
                {"sec_user_ids": json.dumps(chunk, separators=(",", ":"))},
                referer=f"{BASE}/follow")
            if not data or str(data.get("status_code", 0)) != "0":
                if not self.last_error:
                    self.last_error = "im_user_info_rejected"
                continue
            for row in data.get("data") or []:
                if not isinstance(row, dict):
                    continue
                sec_uid = str(row.get("sec_uid") or row.get("secUid") or "")
                if not sec_uid:
                    continue
                profiles[sec_uid] = {
                    "sec_uid": sec_uid,
                    "nickname": str(row.get("nickname")
                                    or row.get("alias_nickname") or ""),
                    "avatar": self._profile_avatar(row),
                }
        return profiles

    async def fetch_dm_conversations(self) -> List[dict]:
        """纯协议读取当前账号会话快照并补齐对端资料。"""
        from ...browser.douyin_im_pb import (
            GET_MESSAGE_BY_INIT_URL,
            build_init_request,
            parse_conversations,
            parse_send_response,
        )

        self.last_error = ""
        owned = self._session is None
        cli = self._session or AsyncSession(impersonate=self.impersonate)
        try:
            body = build_init_request(
                int(time.time() * 1_000_000), **self._im_request_options())
            response = await cli.post(
                GET_MESSAGE_BY_INIT_URL, data=body,
                headers={**self._headers(),
                         "Accept": "application/x-protobuf",
                         "Content-Type": "application/x-protobuf",
                         "Origin": BASE},
                impersonate=self.impersonate, timeout=self.timeout,
                proxy=self.proxy or None)
            raw = response.content
            if response.status_code != 200 or not raw:
                self.last_error = (f"http_{response.status_code}"
                                   if response.status_code != 200 else "empty_body")
                return []
            status = parse_send_response(raw)
            if not status.get("ok") or status.get("cmd") != 2043:
                self.last_error = (self._im_rejection(status)
                                   if status.get("msg") or status.get("error_code")
                                   else "imapi_invalid_response")
                return []
            conversations = parse_conversations(raw)
            profiles = await self.fetch_im_user_profiles(
                [row.get("peer_sec_uid", "") for row in conversations])
            # 资料水合失败不应丢掉已经成功解析的会话快照。
            if conversations:
                self.last_error = ""
            out: List[dict] = []
            for row in conversations:
                profile = profiles.get(row.get("peer_sec_uid", ""), {})
                out.append({
                    "conv_id": row["conv_id"],
                    "peer_uid": row["peer_uid"],
                    "peer_sec_uid": row.get("peer_sec_uid")
                    or profile.get("sec_uid", ""),
                    "peer_nickname": profile.get("nickname", ""),
                    "peer_avatar": profile.get("avatar", ""),
                    "last_text": row.get("last_text", ""),
                    "last_time": row.get("last_time", 0),
                    "unread_count": 0,
                    "conv_short_id": row.get("conv_short_id", ""),
                    "ticket": row.get("ticket", ""),
                    "raw_json": json.dumps({
                        "last_sender_uid": row.get("last_sender_uid", ""),
                        "self_uid": row.get("self_uid", ""),
                        "last_msg_type": row.get("last_msg_type", 0),
                    }, ensure_ascii=False),
                })
            return out
        except Exception as exc:
            self.last_error = f"network:{type(exc).__name__}"
            return []
        finally:
            if owned:
                await cli.close()
                self._ms_token = None

    async def search_awemes_page(self, keyword: str, *, offset: int = 0,
                                 count: int = 20, search_sort: str = "general",
                                 publish_time: str = "all",
                                 content_type: str = "all") -> dict:
        """调用抖音网页搜索接口，返回原始作品卡片及分页状态。"""
        from ...browser.fetcher import extract_search_awemes
        sort_codes = {"general": "0", "most_liked": "1", "latest": "2"}
        time_codes = {"all": "0", "day": "1", "week": "7", "half_year": "180"}
        params = {
            "keyword": keyword,
            "search_channel": "aweme",
            "search_source": "normal_search",
            "query_correct_type": "1",
            "is_filter_search": "0",
            "offset": max(0, int(offset or 0)),
            "count": max(1, min(50, int(count or 20))),
            "sort_type": sort_codes.get(search_sort, "0"),
            "publish_time": time_codes.get(publish_time, "0"),
            "type": "video" if content_type == "video" else "general",
        }
        data = await self._get_json(
            "/aweme/v1/web/general/search/single/", params,
            referer=f"{BASE}/search/{urllib.parse.quote(keyword, safe='')}")
        if not data:
            return {"items": [], "has_more": False, "offset": offset,
                    "error": self.last_error or "empty_response"}
        nil_info = data.get("search_nil_info") or {}
        marker = " ".join(str(nil_info.get(k) or "") for k in
                          ("search_nil_type", "search_nil_item", "text_type"))
        if any(word in marker.casefold() for word in ("verify", "captcha", "risk")):
            return {"items": [], "has_more": False, "offset": offset,
                    "error": "verification_required"}
        items = extract_search_awemes(data)
        return {"items": items,
                "has_more": bool(data.get("has_more") or data.get("has_more_item")),
                "offset": int(data.get("offset") or (offset + len(items))),
                "error": ""}

    async def search_awemes(self, keyword: str, *, max_results: int = 20,
                            max_pages: int = 3, search_sort: str = "general",
                            publish_time: str = "all", content_type: str = "all",
                            min_likes: int = 0, min_comments: int = 0) -> tuple[List[dict], str]:
        """分页搜索并做本地筛选；返回 (作品列表, 可回退原因)。"""
        import time
        found: Dict[str, dict] = {}
        offset = 0
        for _ in range(max(1, min(10, int(max_pages or 1)))):
            page = await self.search_awemes_page(
                keyword, offset=offset, count=min(50, max_results),
                search_sort=search_sort, publish_time=publish_time,
                content_type=content_type)
            if page.get("error"):
                return list(found.values())[:max_results], str(page["error"])
            for item in page.get("items") or []:
                stats = item.get("statistics") or {}
                if content_type == "video" and not item.get("video"):
                    continue
                if content_type == "images" and not item.get("images"):
                    continue
                if int(stats.get("digg_count") or 0) < max(0, int(min_likes or 0)):
                    continue
                if int(stats.get("comment_count") or 0) < max(0, int(min_comments or 0)):
                    continue
                if publish_time != "all":
                    windows = {"day": 86400, "week": 7 * 86400, "half_year": 180 * 86400}
                    created = int(item.get("create_time") or 0)
                    if created and created < time.time() - windows.get(publish_time, 0):
                        continue
                aid = str(item.get("aweme_id") or "")
                if aid:
                    found.setdefault(aid, item)
            if len(found) >= max_results or not page.get("has_more"):
                break
            next_offset = int(page.get("offset") or offset + len(page.get("items") or []))
            if next_offset <= offset:
                break
            offset = next_offset
        items = list(found.values())[:max_results]
        if search_sort == "latest":
            items.sort(key=lambda x: int(x.get("create_time") or 0), reverse=True)
        elif search_sort == "most_liked":
            items.sort(key=lambda x: int((x.get("statistics") or {}).get("digg_count") or 0), reverse=True)
        return items, ("empty_response" if not items else "")

    async def fetch_all_video_list(self, sec_uid: str, max_pages: int = 10,
                                   count: int = 20) -> List[dict]:
        """读取账号公开作品，供本账号作品同步和健康监控复用。"""
        out: List[dict] = []
        cursor = 0
        seen: Set[str] = set()
        for _ in range(max(1, min(30, int(max_pages or 1)))):
            page = await self.fetch_video_list(sec_uid, cursor, count)
            items = page.get("items") or []
            if not items:
                break
            for item in items:
                aid = str(item.get("aweme_id") or "")
                if aid and aid not in seen:
                    seen.add(aid)
                    out.append(item)
            if not page.get("has_more"):
                break
            nxt = int(page.get("max_cursor") or 0)
            if nxt == cursor:
                break
            cursor = nxt
        return out

    # ── 用户资料(对应 NativeClient.FetchProfile)──
    async def fetch_self_profile(self) -> Optional[dict]:
        """读取当前 Cookie 对应账号，不依赖已知 sec_uid。"""
        data = await self._get_json(
            "/aweme/v1/web/user/profile/self/", {}, referer=f"{BASE}/user/self")
        if data and data.get("user"):
            return data["user"]
        if data and data.get("status_code") not in (None, 0):
            self.last_error = f"status_{data.get('status_code')}"
        elif data is not None and not self.last_error:
            self.last_error = "missing_user"
        return None

    async def fetch_profile(self, sec_uid: str) -> Optional[dict]:
        data = await self._get_json(
            "/aweme/v1/web/user/profile/other/",
            {"sec_user_id": sec_uid, "publish_video_strategy_type": "2"},
            referer=f"{BASE}/user/{sec_uid}",
        )
        if data and data.get("user"):
            return data["user"]
        return None

    async def resolve_user_identifier(
            self, identifier: str) -> tuple[Optional[dict], str]:
        """Resolve a visible Douyin ID to the numeric UID used by IM.

        A bare numeric value is not assumed to be an IM UID. Douyin exposes a
        separate visible ``unique_id``/``short_id`` namespace, so new-message
        flows must exact-match the first-party user-search response before
        building cmd=609.
        """
        raw = str(identifier or "").strip().lstrip("@")
        if not raw:
            return None, "缺少抖音号"
        try:
            parsed_url = urllib.parse.urlsplit(raw)
            if parsed_url.scheme in {"http", "https"}:
                if not ((parsed_url.hostname or "").casefold() == "douyin.com"
                        or (parsed_url.hostname or "").casefold().endswith(
                            ".douyin.com")):
                    return None, "目标主页不是 douyin.com"
                match = re.search(r"/user/([^/?#]+)", parsed_url.path)
                raw = urllib.parse.unquote(match.group(1)) if match else ""
        except ValueError:
            return None, "目标主页格式无效"
        if not raw:
            return None, "目标主页缺少用户标识"

        if raw.startswith("MS4wLjAB"):
            profile = await self.fetch_profile(raw)
            if not profile:
                return None, self.last_error or "sec_uid 查询无结果"
            return self._resolved_user(profile), ""

        payload = await self._get_json(
            "/aweme/v1/web/discover/search/",
            {
                "keyword": raw,
                "search_channel": "aweme_user_web",
                "search_source": "normal_search",
                "query_correct_type": "1",
                "is_filter_search": "0",
                "offset": 0,
                "count": 20,
            },
            referer=f"{BASE}/search/{urllib.parse.quote(raw, safe='')}?type=user",
        )
        if not payload:
            return None, self.last_error or "用户搜索无响应"
        containers = [payload]
        for key in ("data", "result"):
            if isinstance(payload.get(key), dict):
                containers.append(payload[key])
        rows: list = []
        for container in containers:
            for key in ("user_list", "users", "data"):
                value = container.get(key)
                if isinstance(value, list):
                    rows.extend(value)

        for row in rows:
            if not isinstance(row, dict):
                continue
            user = (row.get("user_info") or row.get("user")
                    or row.get("aweme_user") or row)
            if not isinstance(user, dict):
                continue
            visible_ids = {
                str(user.get("unique_id") or "").strip(),
                str(user.get("short_id") or "").strip(),
            }
            if raw not in visible_ids:
                continue
            resolved = self._resolved_user(user)
            if resolved.get("uid") and resolved.get("sec_uid"):
                return resolved, ""
            return None, "搜索结果缺少 uid/sec_uid"
        return None, "未找到完全匹配的抖音号"

    @staticmethod
    def _resolved_user(user: dict) -> dict:
        return {
            "uid": str(user.get("uid") or user.get("user_id") or ""),
            "sec_uid": str(user.get("sec_uid") or user.get("sec_user_id") or ""),
            "unique_id": str(user.get("unique_id") or ""),
            "short_id": str(user.get("short_id") or ""),
            "nickname": str(user.get("nickname") or ""),
        }

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

    # ── 短视频弹幕(播放页接口,与 comment/list 分开)──
    async def fetch_danmaku_page(self, aweme_id: str, start_time: int = 0,
                                 end_time: int = 0, duration: int = 0) -> dict:
        """请求一个视频时间窗口的弹幕页，浏览器拦截器是主路径。"""
        data = await self._get_json(
            "/aweme/v1/web/danmaku/get_v2/",
            {
                "item_id": aweme_id,
                "aweme_id": aweme_id,
                "start_time": max(0, int(start_time or 0)),
                "end_time": max(0, int(end_time or 0)),
                "duration": max(0, int(duration or 0)),
            },
            referer=f"{BASE}/video/{aweme_id}",
        )
        return data or {}

    async def fetch_all_danmaku(self, aweme_id: str, *, start_time: int = 0,
                                end_time: int = 0, duration: int = 0,
                                max_pages: int = 4) -> List[dict]:
        """读取播放页弹幕并从不同版本的响应结构中提取条目。

        抖音网页端目前通常在一次 ``get_v2`` 响应中返回完整列表；保留
        ``max_pages`` 参数以兼容后续按 cursor/时间窗口拆分的版本。
        """
        out: List[dict] = []
        seen: Set[str] = set()

        def walk(value: Any, depth: int = 0) -> None:
            if depth > 8:
                return
            if isinstance(value, list):
                for row in value:
                    if isinstance(row, dict):
                        # 弹幕条目至少应有文本或稳定 id，避免把分页元数据当条目。
                        if any(row.get(k) not in (None, "") for k in
                               ("content", "text", "danmaku_text", "body",
                                "danmaku_id", "barrage_id", "bullet_id", "cid")):
                            key = str(row.get("danmaku_id") or row.get("barrage_id")
                                      or row.get("bullet_id") or row.get("cid")
                                      or row.get("id") or "")
                            if not key:
                                key = json.dumps(row, ensure_ascii=False, sort_keys=True)
                            if key not in seen:
                                seen.add(key)
                                out.append(row)
                    walk(row, depth + 1)
                return
            if isinstance(value, dict):
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        walk(child, depth + 1)

        for _ in range(max(1, min(10, int(max_pages or 1)))):
            page = await self.fetch_danmaku_page(
                aweme_id, start_time=start_time, end_time=end_time,
                duration=duration)
            before = len(out)
            walk(page)
            if len(out) == before:
                break
            # get_v2 没有稳定公开 cursor，重复窗口只会返回同一批数据。
            break
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
        if not user_id and sec_uid:
            profile = await self.fetch_profile(sec_uid)
            user_id = str((profile or {}).get("uid")
                          or (profile or {}).get("user_id") or "")
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
