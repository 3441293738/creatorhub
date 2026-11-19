"""哔哩哔哩直连 HTTP 客户端（Web / 创作中心 / WBI 签名 / VC 私信 / Cookie 刷新）。"""
from __future__ import annotations

import json
import math
import re
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlencode

from curl_cffi.requests import AsyncSession

from ...browser.manager import normalize_proxy
from ...netfp import impersonate_for_ua
from .._submit import notify_submit
from .extract import (
    Aweme,
    flatten_bili_comments,
    parse_bili_aweme,
    parse_bili_comment,
    parse_bili_creator_comment,
    parse_bili_creator_work,
    parse_bili_danmaku_xml,
    parse_bili_dm_messages,
    parse_bili_dm_sessions,
    parse_bili_self_user,
)
from .formatter import FALLBACK_PARTITIONS, play_source_percent
from .im_codec import create_uuid_v1
from .resolve import av_to_bv, bv_to_av, resolve_bili_work_ref
from .wbi import (
    VC_COMMON_PARAMS,
    extract_refresh_csrf,
    get_correspond_path,
    mixin_key_from_wbi,
    sign_with_key,
)

API_BASE = "https://api.bilibili.com"
MEMBER_BASE = "https://member.bilibili.com"
PASSPORT_BASE = "https://passport.bilibili.com"
VC_BASE = "https://api.vc.bilibili.com"
WWW_BASE = "https://www.bilibili.com"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _domain_is_bili(domain: Any) -> bool:
    if not isinstance(domain, str):
        return False
    if domain == "":
        return True
    clean = domain.lstrip(".").lower()
    return clean == "bilibili.com" or clean.endswith(".bilibili.com")


def cookie_map_from_state(storage_state: str | Mapping[str, Any] | None) -> Dict[str, str]:
    """从 Playwright `storage_state` JSON 或原始 Cookie 字符串提取有效 `.bilibili.com` Cookie。"""
    if storage_state is None:
        return {}
    if isinstance(storage_state, str):
        raw = storage_state.strip()
        if not raw:
            return {}
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, Mapping):
                storage_state = parsed
            else:
                return {}
        else:
            out: Dict[str, str] = {}
            for part in raw.split(";"):
                seg = part.strip()
                if not seg or "=" not in seg:
                    continue
                k, _, v = seg.partition("=")
                k = k.strip()
                if k:
                    out[k] = v.strip()
            return out

    if not isinstance(storage_state, Mapping):
        return {}
    cookies = storage_state.get("cookies")
    if not isinstance(cookies, list):
        return {str(k): str(v) for k, v in storage_state.items() if k and v is not None}

    now = time.time()
    result: Dict[str, str] = {}
    for ck in cookies:
        if not isinstance(ck, Mapping):
            continue
        if not _domain_is_bili(ck.get("domain", "")):
            continue
        name = str(ck.get("name") or "").strip()
        value = str(ck.get("value") or "")
        if not name:
            continue
        expires = ck.get("expires")
        if expires is not None and not isinstance(expires, bool):
            try:
                exp = float(expires)
                if math.isfinite(exp) and exp > 0 and exp <= now:
                    continue
            except (TypeError, ValueError, OverflowError):
                pass
        result[name] = value
    return result


def cookie_str_from_state(storage_state: str | Mapping[str, Any] | None) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookie_map_from_state(storage_state).items())


def has_bili_login_cookies(storage_state: str | Mapping[str, Any] | None) -> bool:
    cks = cookie_map_from_state(storage_state)
    return bool(cks.get("SESSDATA") and cks.get("bili_jct"))


class BilibiliClient:
    """B 站多域统一异步客户端（按账号隔离 Cookie、UA、TLS 指纹与代理）。"""

    def __init__(
        self,
        storage_state: str | Mapping[str, Any] | None = None,
        user_agent: str = "",
        *,
        proxy: str = "",
        timeout: float = 20.0,
        locale: str = "zh-CN",
    ):
        self.cookies: Dict[str, str] = cookie_map_from_state(storage_state)
        self.user_agent = str(user_agent or DEFAULT_UA)
        self.proxy = normalize_proxy(proxy) or None
        self.timeout = float(timeout)
        self.locale = locale or "zh-CN"
        self.impersonate = impersonate_for_ua(self.user_agent)
        self.last_error: str = ""
        self._session: Optional[AsyncSession] = None
        self._mixin_key: str = ""
        self._wbi_fetched_at: float = 0.0
        self._buvid_checked: bool = False
        self._mid: str = str(self.cookies.get("DedeUserID") or "")

    @property
    def csrf(self) -> str:
        return str(self.cookies.get("bili_jct") or "")

    @property
    def self_mid(self) -> str:
        return str(self._mid or self.cookies.get("DedeUserID") or "")

    @asynccontextmanager
    async def session_scope(self):
        if self._session is not None:
            yield self
            return
        async with AsyncSession(impersonate=self.impersonate) as sess:
            self._session = sess
            try:
                yield self
            finally:
                self._session = None

    def _headers(
        self,
        *,
        referer: str = "https://www.bilibili.com/",
        origin: str = "",
        extra: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, str]:
        hdrs = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": f"{self.locale},zh;q=0.9,en;q=0.8",
            "Referer": referer,
            "User-Agent": self.user_agent,
        }
        if origin:
            hdrs["Origin"] = origin
        if self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if extra:
            hdrs.update(extra)
        return hdrs

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        data: Any = None,
        json_body: Any = None,
        referer: str = "https://www.bilibili.com/",
        origin: str = "",
        extra_headers: Optional[Mapping[str, str]] = None,
        quote: str | bool = "",
    ):
        # curl_cffi AsyncSession.request expects `quote: Union[str, Literal[False]] = ""`
        # (passing `quote=True` causes curl_cffi to iterate over a bool and raise TypeError).
        curl_quote: Any = False if quote is False else (quote if isinstance(quote, str) else "")
        headers = self._headers(referer=referer, origin=origin, extra=extra_headers)
        if self._session is not None:
            resp = await self._session.request(
                method,
                url,
                params=dict(params) if params else None,
                data=data,
                json=json_body,
                headers=headers,
                proxy=self.proxy,
                timeout=self.timeout,
                quote=curl_quote,
            )
        else:
            async with AsyncSession(impersonate=self.impersonate) as sess:
                resp = await sess.request(
                    method,
                    url,
                    params=dict(params) if params else None,
                    data=data,
                    json=json_body,
                    headers=headers,
                    proxy=self.proxy,
                    timeout=self.timeout,
                    quote=curl_quote,
                )
        try:
            for k, v in resp.cookies.items():
                if k and v is not None:
                    self.cookies[str(k)] = str(v)
        except Exception:
            pass
        return resp

    async def _get_json(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        referer: str = "https://www.bilibili.com/",
        origin: str = "",
        quote: str | bool = "",
    ) -> Tuple[Optional[dict], str]:
        try:
            resp = await self._request(
                "GET", url, params=params, referer=referer, origin=origin, quote=quote
            )
        except Exception as exc:
            err = f"network:{type(exc).__name__}:{exc}"
            self.last_error = err
            return None, err
        if resp.status_code != 200:
            err = f"http_{resp.status_code}"
            self.last_error = err
            return None, err
        try:
            payload = resp.json()
        except Exception:
            err = "invalid_json"
            self.last_error = err
            return None, err
        return payload if isinstance(payload, dict) else None, ""

    async def _post_write_form(
        self,
        url: str,
        form_data: Mapping[str, Any],
        *,
        referer: str = "https://www.bilibili.com/",
        origin: str = "https://www.bilibili.com",
        on_submit: Optional[Callable[[], object]] = None,
        quote: str | bool = "",
    ) -> Tuple[Optional[dict], str]:
        """执行写请求：发送前触发 `notify_submit`，网络超时或 5xx 统一标记 `write_uncertain:`。"""
        encoded = urlencode(
            [(str(k), "" if v is None else str(v)) for k, v in form_data.items()]
        )
        await notify_submit(on_submit)
        try:
            resp = await self._request(
                "POST",
                url,
                data=encoded,
                referer=referer,
                origin=origin,
                extra_headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                quote=quote,
            )
        except Exception as exc:
            err = f"write_uncertain:请求已发出但响应中断({type(exc).__name__})"
            self.last_error = err
            return None, err
        if resp.status_code >= 500:
            err = f"write_uncertain:服务端返回 HTTP {resp.status_code}，写结果未确认"
            self.last_error = err
            return None, err
        if resp.status_code != 200:
            err = f"http_{resp.status_code}"
            self.last_error = err
            return None, err
        try:
            payload = resp.json()
        except Exception:
            err = "write_uncertain:写请求返回非 JSON，结果未确认"
            self.last_error = err
            return None, err
        if not isinstance(payload, dict):
            return None, "write_uncertain:响应结构异常"
        code = payload.get("code")
        if code == -101:
            return payload, "logged_out:登录态失效，请重新登录"
        if code not in (0, "0"):
            msg = str(payload.get("message") or payload.get("msg") or f"code={code}")
            return payload, msg
        return payload, ""

    async def ensure_buvid(self) -> None:
        """补齐 `buvid3` / `buvid4` 指纹 Cookie，降低公开搜索与空间接口风控率。"""
        if self._buvid_checked or (self.cookies.get("buvid3") and self.cookies.get("buvid4")):
            self._buvid_checked = True
            return
        self._buvid_checked = True
        payload, _ = await self._get_json(f"{API_BASE}/x/frontend/finger/spi")
        data = (payload or {}).get("data") if isinstance(payload, dict) else None
        if isinstance(data, Mapping):
            if data.get("b_3") and not self.cookies.get("buvid3"):
                self.cookies["buvid3"] = str(data["b_3"])
            if data.get("b_4") and not self.cookies.get("buvid4"):
                self.cookies["buvid4"] = str(data["b_4"])

    async def ensure_wbi_key(self, *, force: bool = False) -> str:
        """获取并缓存 WBI `mixin_key`（即使未登录 `code == -101` 时 `nav` 也会返回 `wbi_img`）。"""
        now = time.time()
        if not force and self._mixin_key and (now - self._wbi_fetched_at) < 3600:
            return self._mixin_key
        payload, err = await self._get_json(f"{API_BASE}/x/web-interface/nav", params={"_t": "1"})
        if not payload:
            raise RuntimeError(f"获取 B 站 WBI 密钥失败: {err}")
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        if data.get("mid"):
            self._mid = str(data["mid"])
        wbi_img = data.get("wbi_img") if isinstance(data.get("wbi_img"), Mapping) else {}
        if not wbi_img.get("img_url") or not wbi_img.get("sub_url"):
            raise RuntimeError("B 站 nav 接口未返回 wbi_img")
        self._mixin_key = mixin_key_from_wbi(wbi_img)
        self._wbi_fetched_at = now
        return self._mixin_key

    async def _get_wbi_json(
        self,
        base_url: str,
        params: Mapping[str, Any],
        *,
        referer: str = "https://www.bilibili.com/",
        origin: str = "",
    ) -> Tuple[Optional[dict], str]:
        await self.ensure_buvid()
        try:
            mixin_key = await self.ensure_wbi_key()
        except Exception as exc:
            return None, str(exc)
        signed = sign_with_key(mixin_key, params)
        sep = "&" if "?" in base_url else "?"
        full_url = f"{base_url}{sep}{signed['query']}&w_rid={signed['w_rid']}"
        payload, err = await self._get_json(full_url, referer=referer, origin=origin, quote=False)
        if isinstance(payload, dict) and payload.get("code") in (-403, -352):
            # WBI 跨日轮换或风控时强制刷新一次密钥重试
            try:
                mixin_key = await self.ensure_wbi_key(force=True)
                signed = sign_with_key(mixin_key, params)
                full_url = f"{base_url}{sep}{signed['query']}&w_rid={signed['w_rid']}"
                payload, err = await self._get_json(
                    full_url, referer=referer, origin=origin, quote=False
                )
            except Exception:
                pass
        return payload, err

    # ═══════════ 认证、账号资料与 Cookie 刷新 ═══════════

    async def check_cookie_refresh(self) -> Tuple[dict, str]:
        if not self.csrf:
            return {}, "missing_csrf"
        payload, err = await self._get_json(
            f"{PASSPORT_BASE}/x/passport-login/web/cookie/info",
            params={"csrf": self.csrf},
            referer="https://www.bilibili.com/",
        )
        if err or not payload:
            return {}, err or "empty"
        if payload.get("code") == -101:
            return {}, "logged_out:登录态失效"
        if payload.get("code") != 0:
            return {}, str(payload.get("message") or f"code={payload.get('code')}")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return data, ""

    async def refresh_cookies(self, refresh_token: str) -> Tuple[dict, str]:
        """通过 B 站官方 RSA-OAEP `correspond/1` 握手流程刷新即将过期的会话。"""
        old_token = str(refresh_token or "").strip()
        old_csrf = self.csrf
        if not old_token or not old_csrf:
            return {}, "缺 refresh_token 或 bili_jct"
        info, err = await self.check_cookie_refresh()
        if err:
            return {}, err
        ts = int(info.get("timestamp") or int(time.time() * 1000))
        correspond_path = get_correspond_path(ts)
        try:
            corr_resp = await self._request(
                "GET",
                f"{WWW_BASE}/correspond/1/{correspond_path}",
                referer="https://www.bilibili.com/",
            )
        except Exception as exc:
            return {}, f"correspond_network:{exc}"
        if corr_resp.status_code != 200:
            return {}, f"correspond_http_{corr_resp.status_code}"
        refresh_csrf = extract_refresh_csrf(corr_resp.text)
        if not refresh_csrf:
            return {}, "未提取到 refresh_csrf"

        ref_payload, ref_err = await self._post_write_form(
            f"{PASSPORT_BASE}/x/passport-login/web/cookie/refresh",
            {
                "csrf": old_csrf,
                "refresh_csrf": refresh_csrf,
                "source": "main_web",
                "refresh_token": old_token,
            },
            referer="https://www.bilibili.com/",
            origin="https://www.bilibili.com",
        )
        if ref_err or not ref_payload:
            return {}, ref_err or "refresh_failed"
        data = ref_payload.get("data") if isinstance(ref_payload.get("data"), dict) else {}
        new_token = str(data.get("refresh_token") or "")
        new_csrf = self.csrf or old_csrf
        await self._post_write_form(
            f"{PASSPORT_BASE}/x/passport-login/web/confirm/refresh",
            {"csrf": new_csrf, "refresh_token": old_token},
            referer="https://www.bilibili.com/",
            origin="https://www.bilibili.com",
        )
        return {"refresh_token": new_token, "cookies": dict(self.cookies)}, ""

    async def fetch_self_profile(self) -> Tuple[dict, str]:
        """校验登录态并拉取账号昵称、头像、UID、粉丝/关注数及投稿总数。"""
        nav_payload, err = await self._get_json(
            f"{API_BASE}/x/web-interface/nav", params={"_t": "1"}
        )
        if err or not nav_payload:
            return {}, err or "nav_empty"
        data = nav_payload.get("data") if isinstance(nav_payload.get("data"), Mapping) else {}
        wbi_img = data.get("wbi_img") if isinstance(data.get("wbi_img"), Mapping) else {}
        if wbi_img.get("img_url") and wbi_img.get("sub_url"):
            try:
                self._mixin_key = mixin_key_from_wbi(wbi_img)
                self._wbi_fetched_at = time.time()
            except Exception:
                pass
        if nav_payload.get("code") == -101 or not data.get("isLogin"):
            return {}, "logged_out:登录态失效，请重新登录"
        if nav_payload.get("code") != 0:
            return {}, str(nav_payload.get("message") or f"code={nav_payload.get('code')}")

        mid = str(data.get("mid") or "")
        self._mid = mid

        stat_data: dict = {}
        acct_data: dict = {}
        archive_count = 0

        if mid:
            stat_resp, _ = await self._get_json(
                f"{API_BASE}/x/relation/stat", params={"vmid": mid}
            )
            if isinstance(stat_resp, dict) and stat_resp.get("code") == 0:
                if isinstance(stat_resp.get("data"), dict):
                    stat_data = stat_resp["data"]

        if self.csrf:
            acct_resp, _ = await self._get_json(
                f"{API_BASE}/x/member/web/account", params={"csrf": self.csrf}
            )
            if isinstance(acct_resp, dict) and acct_resp.get("code") == 0:
                if isinstance(acct_resp.get("data"), dict):
                    acct_data = acct_resp["data"]

        _, page_meta, _ = await self.fetch_creator_archives(pn=1, ps=1)
        if isinstance(page_meta, Mapping):
            archive_count = int(page_meta.get("count") or 0)

        return parse_bili_self_user(data, stat_data, acct_data, archive_count), ""

    # ═══════════ 创作中心稿件、分区、话题与统计 ═══════════

    async def fetch_creator_archives(
        self,
        *,
        pn: int = 1,
        ps: int = 20,
        status: str = "is_pubing,pubed,not_pubed",
    ) -> Tuple[List[dict], dict, str]:
        payload, err = await self._get_json(
            f"{MEMBER_BASE}/x/web/archives",
            params={
                "status": status,
                "pn": max(1, int(pn)),
                "ps": max(1, min(50, int(ps))),
                "coop": 1,
                "interactive": 1,
            },
            referer="https://member.bilibili.com/platform/upload-manager/article",
        )
        if err or not payload:
            return [], {}, err or "empty"
        code = payload.get("code")
        if code == -101:
            return [], {}, "logged_out:创作中心登录态失效"
        if code != 0:
            return [], {}, str(payload.get("message") or f"code={code}")
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        audits = data.get("arc_audits") if isinstance(data.get("arc_audits"), list) else []
        page_meta = data.get("page") if isinstance(data.get("page"), dict) else {}
        works = [w for w in (parse_bili_creator_work(a) for a in audits) if w]
        return works, page_meta, ""

    async def fetch_all_creator_archives(
        self, *, max_pages: int = 5, ps: int = 20
    ) -> Tuple[List[dict], str]:
        all_works: List[dict] = []
        seen: set[str] = set()
        for pn in range(1, max(1, int(max_pages)) + 1):
            batch, page_meta, err = await self.fetch_creator_archives(pn=pn, ps=ps)
            if err:
                return all_works, ("" if all_works else err)
            for w in batch:
                iid = str(w.get("item_id") or "")
                if iid and iid not in seen:
                    seen.add(iid)
                    all_works.append(w)
            total = int((page_meta or {}).get("count") or 0)
            if not batch or (total and len(all_works) >= total):
                break
        return all_works, ""

    async def fetch_human_type_list(self) -> List[dict]:
        payload, err = await self._get_json(
            f"{MEMBER_BASE}/x/vupre/web/archive/human/type2/list",
            params={"t": int(time.time() * 1000)},
            referer="https://member.bilibili.com/platform/upload/video/frame",
        )
        if not err and isinstance(payload, dict) and payload.get("code") == 0:
            data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
            tlist = data.get("type_list")
            if isinstance(tlist, list) and tlist:
                return tlist
        return list(FALLBACK_PARTITIONS)

    async def fetch_topic_types(
        self, *, type_pid: int | str = "", title: str = "", pn: int = 0, ps: int = 20
    ) -> List[dict]:
        payload, err = await self._get_json(
            f"{MEMBER_BASE}/x/vupre/web/topic/type",
            params={
                "pn": max(0, int(pn)),
                "ps": max(1, min(50, int(ps))),
                "type_pid": str(type_pid or ""),
                "title": str(title or ""),
                "t": int(time.time() * 1000),
            },
            referer="https://member.bilibili.com/platform/upload/video/frame",
        )
        if err or not isinstance(payload, dict) or payload.get("code") != 0:
            return []
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        topics = data.get("topics")
        return topics if isinstance(topics, list) else []

    async def search_topics(self, keywords: str, *, pn: int = 0, ps: int = 20) -> List[dict]:
        kw = str(keywords or "").strip()
        if not kw:
            return await self.fetch_topic_types(pn=pn, ps=ps)
        payload, err = await self._get_json(
            f"{MEMBER_BASE}/x/vupre/web/topic/search",
            params={
                "keywords": kw,
                "ps": max(1, min(50, int(ps))),
                "offset": max(0, int(pn)) * max(1, min(50, int(ps))),
                "t": int(time.time() * 1000),
            },
            referer="https://member.bilibili.com/platform/upload/video/frame",
        )
        if err or not isinstance(payload, dict) or payload.get("code") != 0:
            return []
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        res = data.get("result") if isinstance(data.get("result"), Mapping) else data
        topics = res.get("topics")
        return topics if isinstance(topics, list) else []

    async def fetch_work_diagnose(self, bvid: str) -> Tuple[dict, str]:
        """拉取单稿件创作中心诊断数据（核心指标、趋势与 5 维流量来源占比）。"""
        bv = str(bvid or "").strip()
        if not bv:
            return {}, "missing_bvid"
        referer = "https://member.bilibili.com/platform/data-up/video-data"
        view_p, err = await self._get_json(
            f"{MEMBER_BASE}/x/creative_tool/data/center/v3/archive/view",
            params={"bvid": bv, "t": int(time.time() * 1000)},
            referer=referer,
        )
        if err or not view_p:
            return {}, err or "empty"
        if view_p.get("code") == -101:
            return {}, "logged_out:创作中心登录态失效"
        view_data = view_p.get("data") if isinstance(view_p.get("data"), dict) else {}

        ov_p, _ = await self._get_json(
            f"{MEMBER_BASE}/x/creative_tool/data/center/archive_diagnose/overview",
            params={"bvid": bv, "t": int(time.time() * 1000)},
            referer=referer,
        )
        tr_p, _ = await self._get_json(
            f"{MEMBER_BASE}/x/creative_tool/data/center/archive_diagnose/trend",
            params={"bvid": bv, "type": "play", "t": int(time.time() * 1000)},
            referer=referer,
        )
        cid = int(view_data.get("cid") or 0)
        sources = []
        if cid > 0:
            gr_p, _ = await self._get_json(
                f"{MEMBER_BASE}/x/creative_tool/data/center/v2/archive/analyze/graph",
                params={"cid": cid, "t": int(time.time() * 1000)},
                referer=referer,
            )
            gdata = (gr_p or {}).get("data") if isinstance(gr_p, dict) else {}
            if isinstance(gdata, Mapping):
                sources = play_source_percent(gdata.get("play_source"))
        return {
            "bvid": bv,
            "view": view_data,
            "overview": (ov_p or {}).get("data") if isinstance(ov_p, dict) else {},
            "trend": (tr_p or {}).get("data") if isinstance(tr_p, dict) else {},
            "play_sources": sources,
        }, ""

    # ═══════════ 公开视频、空间、搜索、评论、弹幕与关系读取 ═══════════

    async def fetch_video_detail(self, work_id: str) -> Tuple[Optional[dict], str]:
        raw = str(work_id or "").strip()
        ref = await resolve_bili_work_ref(raw, self.user_agent, proxy=self.proxy or "")
        if ref and ref.opus_id and not ref.bvid:
            return await self.fetch_opus_detail(ref.opus_id)

        params: Dict[str, Any] = {}
        if ref and ref.bvid:
            params["bvid"] = ref.bvid
        elif raw.upper().startswith("BV1"):
            params["bvid"] = raw
        elif raw.lower().startswith("av") and raw[2:].isdigit():
            params["aid"] = raw[2:]
        elif raw.isdigit() and len(raw) >= 16:
            return await self.fetch_opus_detail(raw)
        elif raw.isdigit():
            params["aid"] = raw
        else:
            return None, f"无法识别的 B 站作品 ID: {raw}"

        payload, err = await self._get_json(f"{API_BASE}/x/web-interface/view", params=params)
        if err or not payload:
            return None, err or "empty"
        if payload.get("code") != 0:
            return None, str(payload.get("message") or f"code={payload.get('code')}")
        data = payload.get("data")
        return (data if isinstance(data, dict) else None), ""

    async def fetch_opus_detail(self, opus_id: str) -> Tuple[Optional[dict], str]:
        await self.ensure_buvid()
        payload, err = await self._get_json(
            f"{API_BASE}/x/polymer/web-dynamic/v1/detail",
            params={"id": str(opus_id), "timezone_offset": "-480"},
            referer=f"https://www.bilibili.com/opus/{opus_id}",
        )
        if err or not payload:
            return None, err or "empty"
        if payload.get("code") != 0:
            return None, str(payload.get("message") or f"code={payload.get('code')}")
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        item = data.get("item") if isinstance(data.get("item"), dict) else None
        return item, ("" if item else "opus_empty")

    @staticmethod
    def _pick_stream_from_play_data(
        data: Mapping[str, Any], *, target_qn: int, allow_dash: bool = True
    ) -> Tuple[str, str, str]:
        qmap = {
            127: "8K",
            120: "4K",
            116: "1080P60",
            112: "1080P+",
            80: "1080P",
            64: "720P",
            32: "480P",
            16: "360P",
        }
        durl = data.get("durl") if isinstance(data.get("durl"), list) else []
        if durl and isinstance(durl[0], Mapping) and durl[0].get("url"):
            actual_qn = data.get("quality")
            label = qmap.get(int(actual_qn or 0), f"qn{actual_qn}" if actual_qn else "")
            return str(durl[0]["url"]), "", label

        if not allow_dash:
            return "", "", ""
        dash = data.get("dash") if isinstance(data.get("dash"), Mapping) else {}
        videos = [
            v for v in (dash.get("video") or [])
            if isinstance(v, Mapping) and (v.get("baseUrl") or v.get("base_url"))
        ]
        if not videos:
            return "", "", ""
        videos.sort(
            key=lambda v: (
                1 if int(v.get("id") or 0) <= int(target_qn) else 0,
                int(v.get("id") or 0) if int(v.get("id") or 0) <= int(target_qn) else -int(v.get("id") or 0),
                1 if (int(v.get("codecid") or 0) == 7 or str(v.get("codecs") or "").startswith("avc")) else 0,
                int(v.get("bandwidth") or 0),
            ),
            reverse=True,
        )
        chosen_v = videos[0]
        v_url = str(chosen_v.get("baseUrl") or chosen_v.get("base_url") or "")
        audios = [
            a for a in (dash.get("audio") or [])
            if isinstance(a, Mapping) and (a.get("baseUrl") or a.get("base_url"))
        ]
        audios.sort(key=lambda a: int(a.get("bandwidth") or 0), reverse=True)
        a_url = str(audios[0].get("baseUrl") or audios[0].get("base_url") or "") if audios else ""
        actual_qn = chosen_v.get("id") or data.get("quality")
        label = qmap.get(int(actual_qn or 0), f"qn{actual_qn}" if actual_qn else "")
        return v_url, a_url, label

    async def fetch_video_streams(
        self, bvid: str, cid: int, *, qn: int = 80
    ) -> Tuple[str, str, str, str]:
        """获取视频直链 `(video_url, audio_url, quality_label, error)`。

        B 站冷调 `/x/player/wbi/playurl` 常返回 `v_voucher` 空流拦截；而真实浏览器首屏加载
        `www.bilibili.com/video/{bvid}/` 内嵌 `window.__playinfo__`（完整 1080P+ DASH 流）以及
        HTML5 接口 `/x/player/playurl?platform=html5&high_quality=1`（720P 单文件 MP4）可稳定直取。
        """
        from ...runtime_tools import find_ffmpeg

        target_qn = int(qn or 80)
        has_ffmpeg = bool(find_ffmpeg())
        referer = f"https://www.bilibili.com/video/{bvid}"

        # 1. 高清模式且本机具备内置 FFmpeg 时，优先走首屏页面 SSR window.__playinfo__ 拿 1080P+ H.264 DASH
        if target_qn >= 80 and has_ffmpeg:
            try:
                await self.ensure_buvid()
                page_resp = await self._request(
                    "GET", f"{WWW_BASE}/video/{bvid}/", referer="https://www.bilibili.com/"
                )
                if getattr(page_resp, "status_code", 0) == 200 and getattr(page_resp, "text", ""):
                    m_pi = re.search(
                        r"window\.__playinfo__\s*=\s*(\{.*?\})\s*</script>", page_resp.text
                    )
                    if m_pi:
                        pi = json.loads(m_pi.group(1))
                        pdata = pi.get("data") if isinstance(pi, Mapping) and isinstance(pi.get("data"), Mapping) else {}
                        v_u, a_u, lbl = self._pick_stream_from_play_data(
                            pdata, target_qn=target_qn, allow_dash=True
                        )
                        if v_u:
                            return v_u, a_u, lbl, ""
            except Exception:
                pass

        # 2. 轻量单文件 HTML5 MP4 接口（无需 WBI，无 v_voucher 拦截，直接返回可播 MP4）
        try:
            await self.ensure_buvid()
            html5_p, html5_err = await self._get_json(
                f"{API_BASE}/x/player/playurl",
                params={
                    "bvid": bvid,
                    "cid": int(cid),
                    "qn": target_qn,
                    "type": "",
                    "otype": "json",
                    "platform": "html5",
                    "high_quality": 1,
                },
                referer=referer,
            )
            if not html5_err and isinstance(html5_p, Mapping) and html5_p.get("code") == 0:
                hdata = html5_p.get("data") if isinstance(html5_p.get("data"), Mapping) else {}
                v_u, a_u, lbl = self._pick_stream_from_play_data(
                    hdata, target_qn=target_qn, allow_dash=has_ffmpeg
                )
                if v_u:
                    return v_u, a_u, lbl, ""
        except Exception:
            pass

        # 3. 回退 WBI playurl (fnval=1 -> fnval=0 -> fnval=16 DASH)
        last_err = ""
        saw_voucher = False
        for fnval, extra in (
            (1, {"platform": "html5", "high_quality": 1}),
            (0, {}),
            (16, {}),
        ):
            if fnval == 16 and not has_ffmpeg:
                continue
            p_wbi, w_err = await self._get_wbi_json(
                f"{API_BASE}/x/player/wbi/playurl",
                {
                    "bvid": bvid,
                    "cid": int(cid),
                    "qn": target_qn,
                    "fnval": fnval,
                    "fnver": 0,
                    "fourk": 1,
                    **extra,
                },
                referer=referer,
            )
            if w_err:
                last_err = w_err
                continue
            if not isinstance(p_wbi, Mapping):
                continue
            if p_wbi.get("code") not in (None, 0):
                last_err = str(p_wbi.get("message") or f"code={p_wbi.get('code')}")
                continue
            wdata = p_wbi.get("data") if isinstance(p_wbi.get("data"), Mapping) else {}
            if wdata.get("v_voucher"):
                saw_voucher = True
            v_u, a_u, lbl = self._pick_stream_from_play_data(
                wdata, target_qn=target_qn, allow_dash=(fnval == 16 and has_ffmpeg)
            )
            if v_u:
                return v_u, a_u, lbl, ""

        if saw_voucher:
            return "", "", "", "B 站返回风控凭据拦截(v_voucher)，请稍后重试"
        return "", "", "", last_err or "未返回可用视频流(durl/dash均为空)"

    async def fetch_video_playurl(
        self, bvid: str, cid: int, *, qn: int = 80
    ) -> Tuple[str, str, str]:
        """兼容单链接返回 `(video_url, quality_label, error)`。"""
        v_url, _a_url, label, err = await self.fetch_video_streams(bvid, cid, qn=qn)
        return v_url, label, err

    async def fetch_aweme_by_work(
        self, work_id: str, *, with_play_url: bool = True, quality: str = "highest"
    ) -> Tuple[Optional[Aweme], str]:
        detail, err = await self.fetch_video_detail(work_id)
        if err or not detail:
            return None, err or "detail_empty"
        if isinstance(detail.get("modules"), Mapping):
            aw = parse_bili_aweme(detail, require_media=False)
            return aw, ("" if aw else "parse_opus_failed")

        bvid = str(detail.get("bvid") or "").strip()
        cid = int(detail.get("cid") or 0)
        if not cid and isinstance(detail.get("pages"), list) and detail["pages"]:
            p0 = detail["pages"][0]
            if isinstance(p0, Mapping):
                cid = int(p0.get("cid") or 0)
        play_url, audio_url, qlabel, play_err = "", "", "", ""
        if with_play_url and bvid and cid > 0:
            qn = 32 if quality == "lowest" else (64 if str(quality) == "720" else 80)
            play_url, audio_url, qlabel, play_err = await self.fetch_video_streams(bvid, cid, qn=qn)
        aw = parse_bili_aweme(
            detail, play_url=play_url, audio_url=audio_url, quality_label=qlabel, require_media=False
        )
        if aw is None:
            return None, "parse_video_failed"
        if with_play_url and not aw.medias and play_err:
            return aw, play_err
        return aw, ""

    async def fetch_user_videos(
        self,
        mid: str,
        *,
        pn: int = 1,
        ps: int = 30,
        order: str = "pubdate",
        keyword: str = "",
    ) -> Tuple[List[dict], bool, str]:
        uid = str(mid or "").strip()
        if not uid:
            return [], False, "missing_mid"
        payload, err = await self._get_wbi_json(
            f"{API_BASE}/x/space/wbi/arc/search",
            {
                "mid": uid,
                "ps": max(1, min(50, int(ps))),
                "tid": 0,
                "pn": max(1, int(pn)),
                "keyword": keyword,
                "order": order or "pubdate",
                "platform": "web",
                "web_location": "1550101",
                "order_avoided": "true",
            },
            referer=f"https://space.bilibili.com/{uid}/video",
        )
        if err or not payload:
            return [], False, err or "empty"
        if payload.get("code") != 0:
            return [], False, str(payload.get("message") or f"code={payload.get('code')}")
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        lst = data.get("list") if isinstance(data.get("list"), Mapping) else {}
        vlist = lst.get("vlist") if isinstance(lst.get("vlist"), list) else []
        page = data.get("page") if isinstance(data.get("page"), Mapping) else {}
        count = int(page.get("count") or 0)
        has_more = (int(pn) * int(ps)) < count if count else (len(vlist) >= int(ps))
        return vlist, has_more, ""

    async def search_videos(
        self,
        keyword: str,
        *,
        page: int = 1,
        page_size: int = 20,
        order: str = "totalrank",
    ) -> Tuple[List[dict], bool, str]:
        kw = str(keyword or "").strip()
        if not kw:
            return [], False, "搜索关键词不能为空"
        payload, err = await self._get_wbi_json(
            f"{API_BASE}/x/web-interface/wbi/search/type",
            {
                "search_type": "video",
                "keyword": kw,
                "page": max(1, int(page)),
                "page_size": max(1, min(50, int(page_size))),
                "order": order or "totalrank",
                "platform": "pc",
            },
            referer="https://search.bilibili.com/",
        )
        if err or not payload:
            return [], False, err or "empty"
        if payload.get("code") != 0:
            return [], False, str(payload.get("message") or f"code={payload.get('code')}")
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        rows = data.get("result") if isinstance(data.get("result"), list) else []
        num_pages = int(data.get("numPages") or 0)
        has_more = int(page) < num_pages if num_pages else bool(rows)
        return rows, has_more, ""

    async def _resolve_reply_target(self, work_id: str) -> Tuple[int, int, str, str]:
        """解析评论目标 `(oid, reply_type, canonical_work_id, error)`。"""
        raw = str(work_id or "").strip()
        ref = await resolve_bili_work_ref(raw, self.user_agent, proxy=self.proxy or "")
        if ref and ref.bvid:
            aid = ref.aid or bv_to_av(ref.bvid)
            return aid, 1, ref.bvid, ""
        if raw.upper().startswith("BV1"):
            bvid = raw[:12]
            return bv_to_av(bvid), 1, bvid, ""
        if raw.lower().startswith("av") and raw[2:].isdigit():
            aid = int(raw[2:])
            return aid, 1, av_to_bv(aid), ""
        if raw.isdigit() and len(raw) < 16:
            aid = int(raw)
            return aid, 1, av_to_bv(aid), ""

        opus_id = ref.opus_id if (ref and ref.opus_id) else raw
        if opus_id.isdigit():
            item, err = await self.fetch_opus_detail(opus_id)
            if err or not item:
                return 0, 0, opus_id, err or "opus_not_found"
            basic = item.get("basic") if isinstance(item.get("basic"), Mapping) else {}
            cid_str = str(basic.get("comment_id_str") or opus_id)
            ctype = int(basic.get("comment_type") or 17)
            if cid_str.isdigit():
                return int(cid_str), ctype, opus_id, ""
        return 0, 0, raw, f"无法解析评论目标 oid: {raw}"

    async def fetch_comments(
        self,
        work_id: str,
        *,
        max_count: int = 50,
        include_replies: bool = True,
    ) -> Tuple[List[dict], str]:
        oid, ctype, canonical_id, rerr = await self._resolve_reply_target(work_id)
        if rerr or oid <= 0:
            return [], rerr or "invalid_oid"

        collected: List[dict] = []
        seen: set[str] = set()
        next_offset = ""
        referer = (
            f"https://www.bilibili.com/video/{canonical_id}"
            if canonical_id.startswith("BV1")
            else f"https://www.bilibili.com/opus/{canonical_id}"
        )

        for page_idx in range(max(1, math.ceil(max_count / 20))):
            pagination = json.dumps({"offset": next_offset}, separators=(",", ":"))
            payload, err = await self._get_wbi_json(
                f"{API_BASE}/x/v2/reply/wbi/main",
                {
                    "oid": oid,
                    "type": ctype,
                    "mode": 3,
                    "plat": 1,
                    "ps": 20,
                    "pagination_str": pagination,
                },
                referer=referer,
            )
            if err or not payload:
                return collected, ("" if collected else (err or "empty"))
            if payload.get("code") != 0:
                msg = str(payload.get("message") or f"code={payload.get('code')}")
                return collected, ("" if collected else msg)

            data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
            flat = flatten_bili_comments(
                data.get("replies"),
                data.get("top_replies") if page_idx == 0 else None,
            )
            for raw_item in flat:
                parsed = parse_bili_comment(raw_item, default_aweme_id=canonical_id)
                if parsed and parsed["comment_id"] not in seen:
                    seen.add(parsed["comment_id"])
                    collected.append(parsed)
                if len(collected) >= max_count:
                    break

            if include_replies and len(collected) < max_count:
                for root_item in (data.get("replies") or []):
                    if not isinstance(root_item, Mapping):
                        continue
                    rcount = int(root_item.get("rcount") or 0)
                    embedded = root_item.get("replies") or []
                    root_rpid = str(root_item.get("rpid_str") or root_item.get("rpid") or "")
                    if root_rpid and rcount > len(embedded) and len(collected) < max_count:
                        sub_p, _ = await self._get_json(
                            f"{API_BASE}/x/v2/reply/reply",
                            params={"oid": oid, "type": ctype, "root": root_rpid, "ps": 20, "pn": 1},
                            referer=referer,
                        )
                        sub_data = (sub_p or {}).get("data") if isinstance(sub_p, dict) else {}
                        for sr in (sub_data.get("replies") or []) if isinstance(sub_data, Mapping) else []:
                            parsed = parse_bili_comment(sr, default_aweme_id=canonical_id)
                            if parsed and parsed["comment_id"] not in seen:
                                seen.add(parsed["comment_id"])
                                collected.append(parsed)
                            if len(collected) >= max_count:
                                break

            if len(collected) >= max_count:
                break
            cursor = data.get("cursor") if isinstance(data.get("cursor"), Mapping) else {}
            p_reply = (
                cursor.get("pagination_reply")
                if isinstance(cursor.get("pagination_reply"), Mapping)
                else {}
            )
            next_offset = str(p_reply.get("next_offset") or "")
            if cursor.get("is_end") or not next_offset:
                break

        return collected, ""

    async def fetch_up_fulllist_comments(
        self,
        *,
        bvid: str = "",
        pn: int = 1,
        ps: int = 20,
        order: int = 1,
    ) -> Tuple[List[dict], bool, str]:
        """拉取创作中心评论管理列表 (`/x/v2/reply/up/fulllist`)。"""
        payload, err = await self._get_json(
            f"{API_BASE}/x/v2/reply/up/fulllist",
            params={
                "order": int(order),
                "filter": -1,
                "type": 1,
                "bvid": str(bvid or ""),
                "pn": max(1, int(pn)),
                "ps": max(1, min(50, int(ps))),
                "charge_plus_filter": "false",
            },
            referer="https://member.bilibili.com/platform/comment/article",
        )
        if err or not payload:
            return [], False, err or "empty"
        if payload.get("code") == -101:
            return [], False, "logged_out:创作中心登录态失效"
        if payload.get("code") != 0:
            return [], False, str(payload.get("message") or f"code={payload.get('code')}")
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        rows = data.get("list") if isinstance(data.get("list"), list) else []
        out = [c for c in (parse_bili_creator_comment(r, default_aweme_id=bvid) for r in rows) if c]
        total = int(data.get("total") or data.get("count") or 0)
        has_more = (int(pn) * int(ps)) < total if total else (len(out) >= int(ps))
        return out, has_more, ""

    async def fetch_danmaku(self, work_id: str) -> Tuple[List[dict], str]:
        detail, err = await self.fetch_video_detail(work_id)
        if err or not detail:
            return [], err or "detail_empty"
        bvid = str(detail.get("bvid") or work_id)
        cid = int(detail.get("cid") or 0)
        if not cid and isinstance(detail.get("pages"), list) and detail["pages"]:
            p0 = detail["pages"][0]
            if isinstance(p0, Mapping):
                cid = int(p0.get("cid") or 0)
        if cid <= 0:
            return [], "missing_cid"
        try:
            resp = await self._request(
                "GET",
                f"{API_BASE}/x/v1/dm/list.so",
                params={"oid": cid},
                referer=f"https://www.bilibili.com/video/{bvid}",
            )
        except Exception as exc:
            return [], f"danmaku_network:{exc}"
        if resp.status_code != 200:
            return [], f"danmaku_http_{resp.status_code}"
        records = parse_bili_danmaku_xml(resp.content, default_aweme_id=bvid)
        return records, ""

    async def fetch_follows(
        self,
        mid: str,
        *,
        kind: str = "following",
        pn: int = 1,
        ps: int = 50,
    ) -> Tuple[List[dict], bool, str]:
        uid = str(mid or self.self_mid or "").strip()
        if not uid:
            return [], False, "missing_mid"
        endpoint = "followers" if kind in {"followers", "follower", "fans"} else "followings"
        payload, err = await self._get_json(
            f"{API_BASE}/x/relation/{endpoint}",
            params={
                "vmid": uid,
                "pn": max(1, int(pn)),
                "ps": max(1, min(50, int(ps))),
                "order": "desc",
            },
            referer=f"https://space.bilibili.com/{uid}/fans/follow",
        )
        if err or not payload:
            return [], False, err or "empty"
        if payload.get("code") == -101:
            return [], False, "logged_out:登录态失效"
        if payload.get("code") != 0:
            return [], False, str(payload.get("message") or f"code={payload.get('code')}")
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        rows = data.get("list") if isinstance(data.get("list"), list) else []
        total = int(data.get("total") or 0)
        is_fan = endpoint == "followers"
        users: List[dict] = []
        for r in rows:
            if not isinstance(r, Mapping):
                continue
            rmid = str(r.get("mid") or "")
            if not rmid:
                continue
            attr = int(r.get("attribute") or 0)
            is_mutual = attr == 6
            is_following = (attr in {2, 6}) if is_fan else True
            face = str(r.get("face") or "")
            if face.startswith("//"):
                face = "https:" + face
            users.append({
                "uid": rmid,
                "user_id": rmid,
                "sec_uid": rmid,
                "unique_id": rmid,
                "nickname": str(r.get("uname") or ""),
                "avatar": face,
                "signature": str(r.get("sign") or ""),
                "follower_count": 0,
                "mutual": is_mutual,
                "is_mutual": is_mutual,
                "is_following": is_following,
            })
        has_more = (int(pn) * int(ps)) < total if total else (len(users) >= int(ps))
        return users, has_more, ""

    # ═══════════ 写操作：评论、点赞、删除、关注、稿件可见性 ═══════════

    async def post_comment(
        self,
        work_id: str,
        message: str,
        *,
        reply_to: str = "",
        root_id: str = "",
        on_submit: Optional[Callable[[], object]] = None,
    ) -> Tuple[bool, str, dict]:
        text = str(message or "").strip()
        if not text:
            return False, "评论内容不能为空", {}
        if not self.csrf:
            return False, "logged_out:缺少 bili_jct Cookie，请重新登录", {}
        oid, ctype, canonical_id, rerr = await self._resolve_reply_target(work_id)
        if rerr or oid <= 0:
            return False, rerr or "invalid_oid", {}

        if reply_to:
            form = {
                "oid": str(oid),
                "type": str(ctype),
                "root": str(root_id or reply_to),
                "parent": str(reply_to),
                "message": text,
                "jsonp": "jsonp",
                "csrf": self.csrf,
            }
        else:
            form = {
                "plat": "1",
                "oid": str(oid),
                "type": str(ctype),
                "message": text,
                "at_name_to_mid": "{}",
                "gaia_source": "main_web",
                "statistics": '{"appId":100,"platform":5}',
                "csrf": self.csrf,
            }
        referer = (
            f"https://www.bilibili.com/video/{canonical_id}"
            if canonical_id.startswith("BV1")
            else f"https://www.bilibili.com/opus/{canonical_id}"
        )
        payload, err = await self._post_write_form(
            f"{API_BASE}/x/v2/reply/add",
            form,
            referer=referer,
            origin="https://www.bilibili.com",
            on_submit=on_submit,
        )
        if err or not payload:
            return False, err or "评论发送失败", {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return True, "", {
            "rpid": str(data.get("rpid_str") or data.get("rpid") or ""),
            "work_id": canonical_id,
        }

    async def like_comment(
        self,
        work_id: str,
        rpid: str,
        *,
        like: bool = True,
        on_submit: Optional[Callable[[], object]] = None,
    ) -> Tuple[bool, str]:
        if not self.csrf:
            return False, "logged_out:缺少 bili_jct Cookie"
        oid, ctype, _, rerr = await self._resolve_reply_target(work_id)
        if rerr or oid <= 0:
            return False, rerr or "invalid_oid"
        _, err = await self._post_write_form(
            f"{API_BASE}/x/v2/reply/action",
            {
                "oid": str(oid),
                "type": str(ctype),
                "rpid": str(rpid),
                "action": "1" if like else "0",
                "jsonp": "jsonp",
                "csrf": self.csrf,
            },
            referer="https://member.bilibili.com/platform/comment/article",
            origin="https://member.bilibili.com",
            on_submit=on_submit,
        )
        return (not err), err

    async def delete_comment(
        self,
        work_id: str,
        rpid: str,
        *,
        on_submit: Optional[Callable[[], object]] = None,
    ) -> Tuple[bool, str]:
        if not self.csrf:
            return False, "logged_out:缺少 bili_jct Cookie"
        oid, ctype, _, rerr = await self._resolve_reply_target(work_id)
        if rerr or oid <= 0:
            return False, rerr or "invalid_oid"
        _, err = await self._post_write_form(
            f"{API_BASE}/x/v2/reply/del",
            {
                "oid": str(oid),
                "type": str(ctype),
                "rpid": str(rpid),
                "jsonp": "jsonp",
                "csrf": self.csrf,
            },
            referer="https://member.bilibili.com/platform/comment/article",
            origin="https://member.bilibili.com",
            on_submit=on_submit,
        )
        return (not err), err

    async def modify_follow(
        self,
        fid: str,
        *,
        follow: bool = True,
        on_submit: Optional[Callable[[], object]] = None,
    ) -> Tuple[bool, str]:
        target = str(fid or "").strip()
        if not target:
            return False, "缺少目标 UP 主 UID"
        if not self.csrf:
            return False, "logged_out:缺少 bili_jct Cookie"
        _, err = await self._post_write_form(
            f"{API_BASE}/x/relation/modify",
            {
                "fid": target,
                "act": "1" if follow else "2",
                "re_src": "11",
                "csrf": self.csrf,
            },
            referer=f"https://space.bilibili.com/{target}",
            origin="https://space.bilibili.com",
            on_submit=on_submit,
        )
        return (not err), err

    async def edit_visibility(
        self,
        aid_or_bvid: str | int,
        *,
        is_only_self: int,
        on_submit: Optional[Callable[[], object]] = None,
    ) -> Tuple[bool, str]:
        if not self.csrf:
            return False, "logged_out:缺少 bili_jct Cookie"
        raw = str(aid_or_bvid or "").strip()
        aid = bv_to_av(raw) if raw.upper().startswith("BV1") else int(raw)
        _, err = await self._post_write_form(
            f"{MEMBER_BASE}/x/vu/web/edit/visibility",
            {
                "aid": str(aid),
                "is_only_self": "1" if int(is_only_self or 0) else "0",
                "csrf": self.csrf,
            },
            referer="https://member.bilibili.com/platform/upload-manager/article",
            origin="https://member.bilibili.com",
            on_submit=on_submit,
        )
        return (not err), err

    # ═══════════ VC 私信（会话列表 / 增量同步 / 历史消息 / WBI 发送） ═══════════

    async def fetch_user_cards(self, uids: Iterable[str]) -> Dict[str, dict]:
        clean = [str(u).strip() for u in uids if str(u).strip() and str(u).strip().isdigit()]
        if not clean:
            return {}
        out: Dict[str, dict] = {}
        for idx in range(0, len(clean), 50):
            chunk = clean[idx : idx + 50]
            payload, err = await self._get_json(
                f"{API_BASE}/x/polymer/pc-electron/v1/user/cards",
                params={"uids": ",".join(chunk)},
                referer="https://message.bilibili.com/",
                origin="https://message.bilibili.com",
            )
            if err or not isinstance(payload, dict) or payload.get("code") != 0:
                continue
            data = payload.get("data")
            if isinstance(data, Mapping):
                for k, v in data.items():
                    if isinstance(v, dict):
                        out[str(k)] = v
        return out

    async def fetch_dm_sessions(self, *, size: int = 100) -> Tuple[List[dict], str]:
        payload, err = await self._get_json(
            f"{VC_BASE}/session_svr/v1/session_svr/get_sessions",
            params={
                "session_type": 1,
                "group_fold": 1,
                "unfollow_fold": 0,
                "sort_rule": 2,
                "size": max(1, min(100, int(size))),
                **VC_COMMON_PARAMS,
            },
            referer="https://message.bilibili.com/",
            origin="https://message.bilibili.com",
        )
        if err or not payload:
            return [], err or "empty"
        if payload.get("code") == -101:
            return [], "logged_out:私信登录态失效"
        if payload.get("code") != 0:
            return [], str(payload.get("message") or f"code={payload.get('code')}")

        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        slist = data.get("session_list") if isinstance(data.get("session_list"), list) else []
        talker_ids = [
            str(s.get("talker_id") or "")
            for s in slist
            if isinstance(s, Mapping) and s.get("talker_id")
        ]
        cards = await self.fetch_user_cards(talker_ids)
        return parse_bili_dm_sessions(payload, cards), ""

    async def fetch_new_dm_sessions(self, begin_ts: int) -> Tuple[List[dict], int, str]:
        """增量拉取 `begin_ts`（微秒）之后更新的会话，用于 Broadcast WS WatchNotify 回调。"""
        payload, err = await self._get_wbi_json(
            f"{VC_BASE}/session_svr/v1/session_svr/new_sessions",
            {"begin_ts": str(max(0, int(begin_ts or 0))), **VC_COMMON_PARAMS},
            referer="https://message.bilibili.com/",
            origin="https://message.bilibili.com",
        )
        if err or not payload:
            return [], begin_ts, err or "empty"
        if payload.get("code") != 0:
            return [], begin_ts, str(payload.get("message") or f"code={payload.get('code')}")
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        slist = data.get("session_list") if isinstance(data.get("session_list"), list) else []
        max_ts = int(begin_ts or 0)
        talker_ids: List[str] = []
        for s in slist:
            if not isinstance(s, Mapping):
                continue
            sts = int(s.get("session_ts") or 0)
            if sts > max_ts:
                max_ts = sts
            tid = str(s.get("talker_id") or "")
            if tid:
                talker_ids.append(tid)
        cards = await self.fetch_user_cards(talker_ids)
        return parse_bili_dm_sessions(payload, cards), max_ts, ""

    async def fetch_dm_messages(
        self,
        talker_id: str,
        *,
        size: int = 20,
        end_seqno: int = 0,
        begin_seqno: int = 0,
        self_uid: str = "",
    ) -> Tuple[dict, str]:
        peer = str(talker_id or "").strip()
        if not peer:
            return {}, "missing_talker_id"
        payload, err = await self._get_json(
            f"{VC_BASE}/svr_sync/v1/svr_sync/fetch_session_msgs",
            params={
                "talker_id": peer,
                "session_type": 1,
                "size": max(1, min(100, int(size))),
                "begin_seqno": str(begin_seqno) if begin_seqno else "",
                "end_seqno": str(end_seqno) if end_seqno else "",
                **VC_COMMON_PARAMS,
            },
            referer="https://message.bilibili.com/",
            origin="https://message.bilibili.com",
        )
        if err or not payload:
            return {}, err or "empty"
        if payload.get("code") == -101:
            return {}, "logged_out:私信登录态失效"
        if payload.get("code") != 0:
            return {}, str(payload.get("message") or f"code={payload.get('code')}")
        return parse_bili_dm_messages(
            payload,
            peer_uid=peer,
            self_uid=str(self_uid or self.self_mid),
            size=size,
        ), ""

    async def send_dm(
        self,
        receiver_id: str,
        text: str,
        *,
        sender_uid: str = "",
        dev_id: str = "",
        on_submit: Optional[Callable[[], object]] = None,
    ) -> Tuple[bool, str, dict]:
        """通过 WBI 签名查询参数与表单请求发送 B 站私信。"""
        body_text = str(text or "").strip()
        peer = str(receiver_id or "").strip()
        if not peer or not body_text:
            return False, "缺少接收方 UID 或私信内容", {}
        if not self.csrf:
            return False, "logged_out:缺少 bili_jct Cookie，请重新登录", {}

        sender = str(sender_uid or self.self_mid or "").strip()
        if not sender:
            prof, perr = await self.fetch_self_profile()
            if perr:
                return False, perr, {}
            sender = str(prof.get("uid") or prof.get("sec_uid") or "")
        if not sender:
            return False, "缺少当前账号 UID", {}

        try:
            mixin_key = await self.ensure_wbi_key()
        except Exception as exc:
            return False, str(exc), {}

        dev = (str(dev_id or "").strip() or create_uuid_v1()).upper()
        signed = sign_with_key(
            mixin_key,
            {
                "w_sender_uid": sender,
                "w_receiver_id": peer,
                "w_dev_id": dev,
            },
        )
        url = f"{VC_BASE}/web_im/v1/web_im/send_msg?{signed['query']}&w_rid={signed['w_rid']}"
        now_sec = str(int(time.time()))
        form = {
            "msg[sender_uid]": sender,
            "msg[receiver_type]": "1",
            "msg[receiver_id]": peer,
            "msg[msg_type]": "1",
            "msg[msg_status]": "0",
            "msg[content]": json.dumps({"content": body_text}, ensure_ascii=False),
            "msg[new_face_version]": "0",
            "msg[canal_token]": "",
            "msg[dev_id]": dev,
            "msg[timestamp]": now_sec,
            "from_firework": "0",
            "build": "0",
            "mobi_app": "web",
            "csrf_token": self.csrf,
            "csrf": self.csrf,
        }
        payload, err = await self._post_write_form(
            url,
            form,
            referer="https://message.bilibili.com/",
            origin="https://message.bilibili.com",
            on_submit=on_submit,
            quote=False,
        )
        if err or not payload:
            return False, err or "私信发送失败", {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return True, "", {
            "msg_key": str(data.get("msg_key") or ""),
            "msg_content": data.get("msg_content"),
        }
