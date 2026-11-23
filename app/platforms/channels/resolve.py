"""微信视频号 (WeChat Channels / Finder) 链接、ID 与公开分享元数据解析。

覆盖以下真实形态:
  1. 公开分享短链: `https://weixin.qq.com/sph/<SHARE_ID>` 与
     `https://channels.weixin.qq.com/finder-preview/pages/sph?id=<SHARE_ID>`
  2. 预览页落地链: `https://channels.weixin.qq.com/finder-preview/pages/feed?eid=<EID>&token=<TOKEN>`
  3. 微信原生 Finder 页: `https://channels.weixin.qq.com/web/pages/feed?oid=<OID>&nid=<NID>&eid=<EID>`
     (其中 `oid` / `nid` 支持无填充 URL-safe Base64 8字节大端 uint64 编码或十进制文本)
  4. 创作者标识: `v2_<hex>@finder`、`sph...` 或 `/web/pages/profile?username=...`
  5. 公开预览接口: `POST https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info`
     (绝不拿封面 URL 冒充视频直链；严格保留 64 位十进制字符串 ID)。
"""
from __future__ import annotations

import base64
import html
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urlsplit

from curl_cffi.requests import AsyncSession

from ...netfp import impersonate_for_ua

PREVIEW_API = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"

# finderUsername: v2_ 开头的十六进制串 + @finder，或 sph 开头账号标识
_FINDER_V2_RE = re.compile(r"\b(v2_[0-9a-fA-F]{16,})(?:@finder)?\b")
_FINDER_SPH_RE = re.compile(r"(?<![/\w])(sph[0-9A-Za-z_]{5,})\b")
# 视频号作品 objectId: 16~20 位无符号 64 位十进制整数串
_OBJECT_RE = re.compile(r"(?<!\d)(\d{16,20})(?!\d)")
_EXPORT_RE = re.compile(r"\b(export/[A-Za-z0-9_+=-]{12,}|exportkey/[A-Za-z0-9_%+=-]{12,})")
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"'`，。！？；、【】《》「」『』]+", re.IGNORECASE)
_BARE_ID_RE = re.compile(r"^[0-9A-Za-z_@.-]{8,200}$")
_SPH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_UINT64_MAX = (1 << 64) - 1


class ChannelsPreviewError(RuntimeError):
    """视频号公开分享预览接口请求或响应异常。"""


@dataclass(frozen=True)
class ChannelsFeedRef:
    kind: str  # "share" | "feed" | "preview_feed" | "object" | "creator"
    share_id: str = ""
    object_id: str = ""
    object_nonce_id: str = ""
    export_id: str = ""
    general_token: str = ""
    username: str = ""
    url: str = ""


@dataclass(frozen=True)
class ChannelsCreatorResolution:
    """视频号创作者解析结果（含可选反查得到的昵称、头像与具体错误提示）。"""

    username: str = ""
    nickname: str = ""
    avatar: str = ""
    source_kind: str = ""
    error: str = ""


def encode_uint64_base64(value: str | int) -> str:
    """将十进制 uint64 编码为视频号 URL 使用的 8 字节大端无填充 URL-safe Base64。"""
    if isinstance(value, (bool, float)) or value is None:
        return ""
    text = str(value).strip()
    if not text.isdecimal():
        return ""
    num = int(text)
    if not 0 <= num <= _UINT64_MAX:
        return ""
    raw = num.to_bytes(8, byteorder="big", signed=False)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_base64_to_uint64(token: str) -> str:
    """将视频号 `/web/pages/feed` 参数中的 URL-safe Base64 解码为十进制 uint64 字符串。"""
    s = (token or "").strip()
    if not s:
        return ""
    if s.isdecimal():
        val = int(s)
        return str(val) if 0 <= val <= _UINT64_MAX else ""
    # 带下划线复合 nonce (例如 2047756189125515680_0_140_0_0) 原样保留，不做 Base64 解码
    if "_" in s and s.split("_", 1)[0].isdecimal():
        return s
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}={0,2}", s):
        return ""
    padded = s + "=" * ((4 - len(s) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception:
        return ""
    if not raw or len(raw) > 8:
        return ""
    if len(raw) < 8:
        raw = b"\x00" * (8 - len(raw)) + raw
    return str(int.from_bytes(raw, byteorder="big", signed=False))


def build_finder_feed_url(
    object_id: str,
    object_nonce_id: str = "",
    *,
    export_id: str = "",
) -> str:
    """用精确十进制 `object_id` 与 `object_nonce_id` 构造规范 `/web/pages/feed` 链接。"""
    oid = (object_id or "").strip()
    nid = (object_nonce_id or "").strip()
    parts: list[str] = []
    if oid.isdecimal():
        b64_oid = encode_uint64_base64(oid)
        if b64_oid:
            parts.append(f"oid={quote(b64_oid, safe='')}")
    if nid:
        if nid.isdecimal():
            b64_nid = encode_uint64_base64(nid)
            parts.append(f"nid={quote(b64_nid or nid, safe='')}")
        else:
            parts.append(f"nid={quote(nid, safe='')}")
    if export_id:
        parts.append(f"eid={quote(export_id.strip(), safe='')}")
    if not parts:
        return ""
    return "https://channels.weixin.qq.com/web/pages/feed?" + "&".join(parts)


def normalize_finder_username(raw: str) -> str:
    """规范化 `v2_...@finder` 用户名。"""
    val = (raw or "").strip()
    if not val:
        return ""
    m = _FINDER_V2_RE.search(val)
    if m:
        return f"{m.group(1)}@finder"
    m_sph = _FINDER_SPH_RE.search(val)
    if m_sph:
        return m_sph.group(1)
    if val.endswith("@finder") and len(val) > 8:
        return val
    return ""


def is_channels_share_url(url: str) -> bool:
    """判断 URL 是否属于微信视频号分享/预览/原生作品页(排除腾讯视频 v.qq.com 等无关域名)。"""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    path = parts.path or ""
    if host == "channels.weixin.qq.com":
        return True
    if host in {"weixin.qq.com", "www.weixin.qq.com"} and path.startswith("/sph/"):
        return True
    return False


def _parse_single_url(raw_url: str) -> Optional[ChannelsFeedRef]:
    candidate = (raw_url or "").strip().rstrip(".,!?;:，。！？；：）】》」』")
    if not candidate:
        return None
    try:
        parsed = urlsplit(unquote(candidate) if "%2F" in candidate.upper() and "://" not in candidate else candidate)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path or ""
    query = parse_qs(parsed.query, keep_blank_values=False)

    if host in {"weixin.qq.com", "www.weixin.qq.com"} and path.startswith("/sph/"):
        sid = path[len("/sph/") :].strip("/")
        if _SPH_ID_RE.fullmatch(sid):
            canonical = f"https://weixin.qq.com/sph/{sid}"
            return ChannelsFeedRef(kind="share", share_id=sid, url=canonical)

    if host == "channels.weixin.qq.com":
        if path == "/finder-preview/pages/sph":
            sid = (query.get("id") or [""])[0].strip()
            if _SPH_ID_RE.fullmatch(sid):
                return ChannelsFeedRef(
                    kind="share",
                    share_id=sid,
                    url=f"https://weixin.qq.com/sph/{sid}",
                )
        if path == "/finder-preview/pages/feed":
            eid = (query.get("eid") or query.get("exportId") or [""])[0].strip()
            token = (query.get("token") or query.get("generalToken") or [""])[0].strip()
            if eid:
                return ChannelsFeedRef(
                    kind="preview_feed",
                    export_id=eid,
                    general_token=token,
                    url=candidate,
                )
        if path.startswith("/web/pages/feed"):
            raw_oid = (query.get("oid") or query.get("objectId") or [""])[0].strip()
            raw_nid = (query.get("nid") or query.get("objectNonceId") or [""])[0].strip()
            eid = (query.get("eid") or query.get("exportId") or [""])[0].strip()
            oid = decode_base64_to_uint64(raw_oid) if raw_oid else ""
            if oid and "_" in oid:
                oid = oid.split("_", 1)[0]
            nid = decode_base64_to_uint64(raw_nid) if raw_nid else ""
            if oid or eid:
                return ChannelsFeedRef(
                    kind="feed",
                    object_id=oid if oid.isdecimal() else "",
                    object_nonce_id=nid,
                    export_id=eid,
                    url=candidate,
                )
        if path.startswith("/web/pages/profile") or path.startswith("/web/pages/author"):
            uname = (
                query.get("username")
                or query.get("finderUsername")
                or query.get("finder_username")
                or query.get("author")
                or [""]
            )[0].strip()
            norm = normalize_finder_username(uname) or uname
            if norm:
                return ChannelsFeedRef(kind="creator", username=norm, url=candidate)
        # 兼容助手 URL 携带 objectId
        raw_obj = (query.get("objectId") or [""])[0].strip()
        if raw_obj.isdecimal():
            return ChannelsFeedRef(kind="object", object_id=raw_obj, url=candidate)
    return None


def parse_channels_url(text: str) -> Optional[ChannelsFeedRef]:
    """从分享文案、URL 或裸 ID 中解析视频号作品或创作者引用。"""
    text = (text or "").strip()
    if not text:
        return None

    for m in _URL_IN_TEXT_RE.finditer(text):
        ref = _parse_single_url(m.group(0))
        if ref is not None:
            return ref

    if text.startswith(("weixin.qq.com/sph/", "channels.weixin.qq.com/")):
        ref = _parse_single_url("https://" + text)
        if ref is not None:
            return ref

    uname = normalize_finder_username(text)
    if uname:
        return ChannelsFeedRef(kind="creator", username=uname)

    m_exp = _EXPORT_RE.search(text)
    if m_exp:
        return ChannelsFeedRef(kind="preview_feed", export_id=m_exp.group(1))

    m_obj = _OBJECT_RE.search(text)
    if m_obj:
        oid = m_obj.group(1)
        if int(oid) <= _UINT64_MAX:
            return ChannelsFeedRef(kind="object", object_id=oid)

    return None


def _plain_text_from_html(value: str) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", value or "")
    return " ".join(html.unescape(cleaned).split())


def _generate_rid() -> str:
    ts_hex = f"{int(time.time()):x}"
    rnd_hex = "".join(random.choice("0123456789abcdef") for _ in range(8))
    return f"{ts_hex}-{rnd_hex}"


async def fetch_finder_preview_info(
    *,
    share_id: str = "",
    export_id: str = "",
    general_token: str = "",
    user_agent: str = "",
    proxy: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    """调用公开视频号分享预览接口 `get_feed_info` 获取标题、作者、计数及动态 exportId。

    严格遵循 `finder_tool.py` 事实边界:
      - 绝不从 `coverUrl` 伪造视频地址;
      - 明确标注 `video_url_available` 与 `comments_available=False`。
    """
    sid = (share_id or "").strip()
    eid = (export_id or "").strip()
    token = (general_token or "").strip()
    if not sid and not eid:
        raise ChannelsPreviewError("必须提供 share_id 或 export_id")

    from ...browser.manager import normalize_proxy

    normalized_proxy = normalize_proxy(proxy) if proxy else ""
    ua = user_agent or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    if sid:
        api_url = PREVIEW_API
        payload: dict[str, Any] = {"baseReq": {"generalToken": token}, "shortUri": sid}
        referer = f"https://channels.weixin.qq.com/finder-preview/pages/sph?id={quote(sid, safe='')}"
    else:
        rid = _generate_rid()
        api_url = (
            f"{PREVIEW_API}?_rid={rid}"
            "&_pageUrl=https:%2F%2Fchannels.weixin.qq.com%2Ffinder-preview%2Fpages%2Ffeed"
        )
        payload = {"baseReq": {"generalToken": token}, "exportId": eid}
        referer = (
            "https://channels.weixin.qq.com/finder-preview/pages/feed?"
            f"entry_card_type=48&comment_scene=39&appid=0&token={quote(token, safe='')}"
            f"&entry_scene=0&eid={quote(eid, safe='')}"
        )

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": "https://channels.weixin.qq.com",
        "Referer": referer,
        "User-Agent": ua,
    }
    from ...netfp import curl_proxy_url
    norm_proxy = curl_proxy_url(proxy) or None
    async with AsyncSession(
        impersonate=impersonate_for_ua(ua),
        trust_env=False,
        timeout=timeout,
        allow_redirects=True,
        proxy=norm_proxy,
    ) as client:
        resp = await client.post(api_url, json=payload, headers=headers)
    if resp.status_code == 401:
        raise ChannelsPreviewError("视频号公开预览接口返回 HTTP 401(需微信原生会话或有效分享凭证)")
    if resp.status_code not in (200, 201):
        raise ChannelsPreviewError(f"视频号预览接口状态异常: HTTP {resp.status_code}")
    try:
        raw = resp.json()
    except Exception as exc:
        raise ChannelsPreviewError(f"视频号预览接口返回非 JSON 响应: {exc}") from exc

    data = raw.get("data") if isinstance(raw, dict) else None
    data = data if isinstance(data, dict) else {}
    err_code = raw.get("errCode", 0) if isinstance(raw, dict) else -1
    if err_code not in (0, "0", None):
        msg = _plain_text_from_html(str(raw.get("errMsg") or ""))
        raise ChannelsPreviewError(f"视频号预览接口返回错误({err_code}): {msg or '未知错误'}")

    err_obj = data.get("errMsg") if isinstance(data.get("errMsg"), dict) else {}
    if err_obj and (
        err_obj.get("type", 0) not in (0, "0", None)
        or str(err_obj.get("title") or "").strip()
        or str(err_obj.get("content") or "").strip()
    ):
        title_err = _plain_text_from_html(str(err_obj.get("title") or ""))
        content_err = _plain_text_from_html(str(err_obj.get("content") or ""))
        merged_err = ": ".join(x for x in (title_err, content_err) if x)
        raise ChannelsPreviewError(merged_err or f"视频号分享不可用(type={err_obj.get('type')})")

    feed = data.get("feedInfo") if isinstance(data.get("feedInfo"), dict) else {}
    author = data.get("authorInfo") if isinstance(data.get("authorInfo"), dict) else {}
    scene = data.get("sceneInfo") if isinstance(data.get("sceneInfo"), dict) else {}
    if not feed and not author:
        raise ChannelsPreviewError("视频号预览接口未返回有效作品信息")

    desc = str(feed.get("description") or "")
    created = feed.get("createtime")
    created_int = int(created) if isinstance(created, (int, float)) or (isinstance(created, str) and created.isdigit()) else 0
    h264_url = str((feed.get("h264VideoInfo") or {}).get("videoUrl") or "") if isinstance(feed.get("h264VideoInfo"), dict) else ""
    h265_url = str((feed.get("h265VideoInfo") or {}).get("videoUrl") or "") if isinstance(feed.get("h265VideoInfo"), dict) else ""
    video_url = str(feed.get("videoUrl") or "")
    pic_info = feed.get("picInfo") if isinstance(feed.get("picInfo"), list) else []
    dynamic_eid = str(scene.get("dynamicExportId") or eid or "")
    raw_author_uname = str(
        author.get("username")
        or author.get("finderUsername")
        or feed.get("username")
        or feed.get("finderUsername")
        or data.get("finderUsername")
        or ""
    ).strip()
    author_username = normalize_finder_username(raw_author_uname) or raw_author_uname
    raw_oid = str(feed.get("objectId") or feed.get("id") or "").strip()
    object_id = raw_oid if raw_oid.isdecimal() else ""

    return {
        "share_url": f"https://weixin.qq.com/sph/{sid}" if sid else "",
        "share_id": sid,
        "export_id": dynamic_eid,
        "object_id": object_id,
        "title": desc.split("\n", 1)[0].strip() if desc else "",
        "description": desc,
        "author": str(author.get("nickname") or ""),
        "author_username": author_username,
        "author_avatar": str(author.get("headImgUrl") or ""),
        "create_time": created_int,
        "create_time_iso": (
            datetime.fromtimestamp(created_int, timezone(timedelta(hours=8))).isoformat()
            if created_int
            else None
        ),
        "cover_url": str(feed.get("coverUrl") or ""),
        "media_type": int(feed.get("mediaType") or 0),
        "video_url": video_url,
        "h264_video_url": h264_url,
        "h265_video_url": h265_url,
        "pic_info": pic_info,
        "bgm_info": feed.get("bgmInfo") if isinstance(feed.get("bgmInfo"), dict) else {},
        "counts_display": {
            k: feed.get(k)
            for k in ("favCountFmt", "likeCountFmt", "forwardCountFmt", "commentCountFmt")
        },
        "video_url_available": bool(video_url or h264_url or h265_url),
        "image_urls_available": bool(pic_info),
        "comments_available": False,
        "endpoint": PREVIEW_API,
        "raw": raw,
    }


def _extract_creator_from_feed_object(obj: dict[str, Any]) -> tuple[str, str, str]:
    """从 ChannelsObject 中提取创作者 `(username, nickname, avatar)`。"""
    if not isinstance(obj, dict):
        return "", "", ""
    contact = obj.get("contact") if isinstance(obj.get("contact"), dict) else {}
    if not contact and isinstance(obj.get("author"), dict):
        contact = obj["author"]
    if not contact and isinstance(obj.get("finderUser"), dict):
        contact = obj["finderUser"]
    raw_uname = str(
        contact.get("username")
        or contact.get("finderUsername")
        or obj.get("username")
        or obj.get("finderUsername")
        or ""
    ).strip()
    username = normalize_finder_username(raw_uname) or raw_uname
    nickname = str(contact.get("nickname") or contact.get("nickName") or "").strip()
    avatar = str(contact.get("headUrl") or contact.get("headImgUrl") or "").strip()
    return username, nickname, avatar


async def resolve_channels_creator(
    text: str, user_agent: str = "", *, proxy: str = "", cfg: Any = None
) -> ChannelsCreatorResolution:
    """从创作者标识、主页 URL 或单条视频分享链接中解析视频号创作者 `v2_...@finder` / `sph...`。

    当输入为单条作品链接（如 `weixin.qq.com/sph/...`、`/web/pages/feed`、`/finder-preview/pages/feed`）
    时，优先检查公开分享预览，若未包含 `finderUsername` 且本机微信通道可用，则通过
    `ChannelsProtocolClient.fetch_feed_detail` 反查作品作者的 `v2_...@finder`。
    """
    text = (text or "").strip()
    if not text:
        return ChannelsCreatorResolution()

    ref = parse_channels_url(text)
    if ref and ref.kind == "creator" and ref.username:
        return ChannelsCreatorResolution(username=ref.username, source_kind="creator")

    norm = normalize_finder_username(text)
    if norm:
        return ChannelsCreatorResolution(username=norm, source_kind="creator")

    if ref is None and "/" not in text and not text.isdigit() and _BARE_ID_RE.match(text):
        return ChannelsCreatorResolution(username=text, source_kind="creator")

    if ref is None or ref.kind not in {"share", "feed", "preview_feed", "object"}:
        return ChannelsCreatorResolution()

    preview_author = ""
    preview_avatar = ""
    preview_eid = ""
    preview_oid = ""

    if ref.share_id or ref.export_id:
        try:
            preview = await fetch_finder_preview_info(
                share_id=ref.share_id,
                export_id=ref.export_id,
                general_token=ref.general_token,
                user_agent=user_agent,
                proxy=proxy,
                timeout=12.0,
            )
            preview_author = str(preview.get("author") or "").strip()
            preview_avatar = str(preview.get("author_avatar") or "").strip()
            preview_eid = str(preview.get("export_id") or "").strip()
            preview_oid = str(preview.get("object_id") or "").strip()
            preview_uname = str(preview.get("author_username") or "").strip()
            if preview_uname:
                return ChannelsCreatorResolution(
                    username=preview_uname,
                    nickname=preview_author,
                    avatar=preview_avatar,
                    source_kind=ref.kind,
                )
        except Exception:
            pass

    read_mode = (
        str(getattr(getattr(cfg, "engine", None), "channels_read_mode", "auto") or "auto")
        .strip()
        .lower()
        if cfg is not None
        else "auto"
    )
    proto_client = None
    sanitize_bridge_error = None
    if cfg is not None and read_mode != "browser":
        from .client import ChannelsProtocolClient, sanitize_bridge_error as _sanitize_bridge_error

        proto_client = ChannelsProtocolClient.from_config(cfg)
        sanitize_bridge_error = _sanitize_bridge_error

    proto_err = ""
    if proto_client is not None:
        try:
            obj, proto_err = await proto_client.fetch_feed_detail(
                object_id=ref.object_id or preview_oid,
                object_nonce_id=ref.object_nonce_id,
                export_id=preview_eid or ref.export_id,
                share_url=ref.url or text,
                proxy=proxy,
            )
            if obj:
                uname, nick, avatar = _extract_creator_from_feed_object(obj)
                if uname:
                    return ChannelsCreatorResolution(
                        username=uname,
                        nickname=nick or preview_author,
                        avatar=avatar or preview_avatar,
                        source_kind=ref.kind,
                    )
                if not proto_err:
                    proto_err = "作品详情未包含创作者 username"
        except Exception as exc:
            proto_err = (
                sanitize_bridge_error(exc) if sanitize_bridge_error is not None else str(exc)
            )
        finally:
            try:
                await proto_client.close()
            except Exception:
                pass

    author_note = f"（作者：{preview_author}）" if preview_author else ""
    if proto_client is None:
        detail = (
            f"已识别视频号作品{author_note}，但公开分享预览未返回创作者 finderUsername(v2_...@finder)；"
            "请在「设置 → 视频号微信通道」开启一键本机微信代理后再试，或直接粘贴 v2_...@finder"
        )
    else:
        detail = (
            f"已识别视频号作品{author_note}，但通过本机微信通道反查创作者失败：{proto_err}；"
            "请确认桌面微信已打开视频号页面后重试，或直接粘贴 v2_...@finder"
        )
    return ChannelsCreatorResolution(
        nickname=preview_author,
        avatar=preview_avatar,
        source_kind=ref.kind,
        error=detail,
    )


async def resolve_channels_user_id(
    text: str, user_agent: str = "", *, proxy: str = "", cfg: Any = None
) -> Optional[str]:
    """从文本/链接里解析视频号创作者 `v2_...@finder` 或 `sph...` 标识。"""
    res = await resolve_channels_creator(text, user_agent=user_agent, proxy=proxy, cfg=cfg)
    return res.username or None


async def resolve_channels_photo_id(
    text: str, user_agent: str = "", *, proxy: str = ""
) -> Optional[str]:
    """从文本/链接里解析视频号单条作品 `objectId` 或动态 `exportId` / `sph:<id>`。"""
    text = (text or "").strip()
    if not text:
        return None
    ref = parse_channels_url(text)
    if ref is not None:
        if ref.object_id:
            return ref.object_id
        if ref.share_id:
            try:
                info = await fetch_finder_preview_info(
                    share_id=ref.share_id, user_agent=user_agent, proxy=proxy, timeout=12.0
                )
                if info.get("export_id"):
                    return str(info["export_id"])
            except Exception:
                pass
            return f"sph:{ref.share_id}"
        if ref.export_id:
            return ref.export_id
    if "/" not in text and text.isdigit():
        return text
    return None


def looks_like_photo(text: str) -> bool:
    """判断输入更像「单条作品」(分享短链 / feed URL / 长数字 objectId)还是「创作者账号」。"""
    ref = parse_channels_url(text or "")
    if ref is not None:
        return ref.kind in {"share", "feed", "preview_feed", "object"}
    return bool(_OBJECT_RE.search(text or "")) and not bool(normalize_finder_username(text or ""))
