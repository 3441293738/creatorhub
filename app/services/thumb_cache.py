"""全平台封面与头像本地磁盘持久化缓存服务（零重复拉取、防过期、防跨号 CDN IP 泄露）。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import re
import socket
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlsplit
from weakref import WeakValueDictionary

from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlmodel import select

from app import runtime as app_runtime
from app.db import get_session
from app.engine.share_downloader import is_private_url
from app.models import DouyinAccount
from app.netfp import curl_proxies, impersonate_for_ua
from app.profiles import OVERSEAS_PLATFORMS, _pool_urls, pick_ephemeral_proxy

IMAGE_EXTS: tuple[str, ...] = (".jpg", ".jpeg", ".webp", ".png", ".gif", ".avif")
_CTYPE_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/avif": ".avif",
}
_EXT_TO_CTYPE: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".png": "image/png",
    ".gif": "image/gif",
    ".avif": "image/avif",
}
_PLATFORM_REFERERS: dict[str, str] = {
    "douyin": "https://www.douyin.com/",
    "xhs": "https://www.xiaohongshu.com/",
    "rednote": "https://www.rednote.com/",
    "kuaishou": "https://www.kuaishou.com/",
    "shipinhao": "https://channels.weixin.qq.com/",
    "tiktok": "https://www.tiktok.com/",
    "bilibili": "https://www.bilibili.com/",
    "instagram": "https://www.instagram.com/",
    "twitter": "https://x.com/",
}

_fetch_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_prewarm_sem = asyncio.Semaphore(4)
_prewarm_inflight: set[str] = set()

IMMUTABLE_CACHE_HEADERS = {
    "Cache-Control": "private, max-age=604800, immutable",
}


def _thumbs_root() -> Path:
    media_dir = getattr(getattr(app_runtime.cfg, "engine", None), "media_dir", "./data/media")
    root = Path(media_dir).expanduser().resolve().parent / "thumbs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def detect_cdn_platform(url: str, hint: str = "") -> str:
    pf = str(hint or "").strip().lower()
    if pf in _PLATFORM_REFERERS:
        return pf
    try:
        host = (urlsplit(url).hostname or "").lower().strip(".")
    except ValueError:
        host = ""
    if not host:
        return "douyin"
    if host == "twimg.com" or host.endswith(".twimg.com") or host.endswith(".x.com") or host.endswith(".twitter.com"):
        return "twitter"
    if host.endswith((".cdninstagram.com", ".fbcdn.net", ".instagram.com")):
        return "instagram"
    if "tiktokcdn" in host or host.endswith(".tiktok.com"):
        return "tiktok"
    if host.endswith((".rednotecdn.com", ".rednote.com")):
        return "rednote"
    if host.endswith((".xhscdn.com", ".xiaohongshu.com")):
        return "xhs"
    if host.endswith((".hdslb.com", ".biliimg.com", ".bilibili.com")):
        return "bilibili"
    if host.endswith((".kwimgs.com", ".yximgs.com", ".kuaishou.com")):
        return "kuaishou"
    if host.endswith((".qpic.cn", ".qlogo.cn", ".qq.com")):
        return "shipinhao"
    if host.endswith((".douyinpic.com", ".byteimg.com", ".pstatp.com", ".snssdk.com", ".douyin.com")):
        return "douyin"
    return pf or "douyin"


def local_thumb_api_url(raw_url: str, *, platform: str = "") -> str:
    """将远程封面/头像 URL 包装为本地持久化缓存接口地址；已是本地 /api/ 路径则原样返回。"""
    u = str(raw_url or "").strip()
    if not u:
        return ""
    if u.startswith("/api/"):
        return u
    if not u.lower().startswith(("http://", "https://")):
        return ""
    pf = detect_cdn_platform(u, platform)
    return f"/api/thumb?url={quote(u, safe='')}&pf={quote(pf, safe='')}"


def _url_cache_key(raw_url: str) -> str:
    return hashlib.sha256(raw_url.strip().encode("utf-8")).hexdigest()


def cached_thumb_file(raw_url: str) -> Path | None:
    """O(1) 检查某远程图片是否已缓存在本地磁盘。"""
    u = str(raw_url or "").strip()
    if not u:
        return None
    key = _url_cache_key(u)
    bucket = _thumbs_root() / key[:2]
    if not bucket.is_dir():
        return None
    for ext in IMAGE_EXTS:
        cand = bucket / f"{key}{ext}"
        try:
            if cand.is_file() and cand.stat().st_size > 0:
                return cand
        except OSError:
            continue
    return None


def find_work_local_cover(local_path: str, aweme_id: str = "") -> Path | None:
    """优先从作品已下载的本地目录中定位封面图或图集首图（零网络开销）。"""
    raw = str(local_path or "").strip()
    if not raw:
        return None
    try:
        p = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    aid = str(aweme_id or "").strip()
    if p.is_file():
        # 1) 若是视频文件（如 xxx.mp4），优先检查同目录旁路保存的 xxx.cover.jpg / {aweme_id}_*.cover.*
        for ext in IMAGE_EXTS:
            sidecar = p.with_name(f"{p.stem}.cover{ext}")
            try:
                if sidecar.is_file() and sidecar.stat().st_size > 0:
                    return sidecar
            except OSError:
                pass
        if aid and p.parent.is_dir():
            try:
                for f in sorted(p.parent.glob(f"{aid}_*.cover.*")):
                    if f.is_file() and f.suffix.lower() in IMAGE_EXTS and f.stat().st_size > 0:
                        return f
            except OSError:
                pass
        # 2) 若该文件本身就是已下载的图片（如单图或图集首图 xxx_1.jpg），直接作为封面返回
        try:
            if p.suffix.lower() in IMAGE_EXTS and p.stat().st_size > 0:
                return p
        except OSError:
            pass
        return None

    if p.is_dir():
        pattern = f"{aid}_*" if aid else "*"
        try:
            entries = [f for f in p.glob(pattern) if f.is_file() and f.suffix.lower() in IMAGE_EXTS and f.stat().st_size > 0]
        except OSError:
            return None
        if not entries:
            return None
        # 优先显式 .cover. 文件，其次按图集序号取第 1 张图
        for f in sorted(entries, key=lambda x: x.name.lower()):
            if ".cover." in f.name.lower():
                return f

        def _idx_key(f: Path) -> tuple[int, int | str]:
            tail = f.stem.rsplit("_", 1)[-1]
            return (0, int(tail)) if tail.isdigit() else (1, f.name.lower())

        return sorted(entries, key=_idx_key)[0]

    return None


_XHS_SIGNED_PATH_RE = re.compile(
    r"^https?://([^/]+)/(\d{12})/[0-9a-fA-F]{32}/([^?#]+)"
)


def xhs_unsigned_cdn_urls(raw_url: str, platform: str = "") -> tuple[list[str], bool]:
    """针对小红书/海外小红书带时间戳签名的图片链接(/YYYYMMDDHHMM/<md5>/<key>)，提取无签名永久镜像地址。

    当签名时间戳已过期(如历史入库记录返回 HTTP 403)时，返回 (mirrors, True) 以便直接优先请求永久镜像。
    """
    u = str(raw_url or "").strip()
    m = _XHS_SIGNED_PATH_RE.match(u)
    if not m:
        return [], False
    host = m.group(1).lower().strip(".")
    if not host.endswith((".rednotecdn.com", ".xhscdn.com")):
        return [], False
    ts_str = m.group(2)
    obj_key = m.group(3).split("!", 1)[0].lstrip("/")
    if not obj_key:
        return [], False
    now_cst = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d%H%M")
    is_expired = ts_str < now_cst
    pf = str(platform or "").strip().lower()
    if pf == "rednote" or host.endswith(".rednotecdn.com"):
        mirrors = [
            f"https://sns-na-i1.xhscdn.com/{obj_key}",
            f"https://sns-img-hw.xhscdn.com/{obj_key}",
            f"https://ci.xiaohongshu.com/{obj_key}",
        ]
    else:
        mirrors = [
            f"https://sns-img-bd.xhscdn.com/{obj_key}",
            f"https://sns-img-hw.xhscdn.com/{obj_key}",
            f"https://ci.xiaohongshu.com/{obj_key}",
        ]
    return mirrors, is_expired


def _validate_public_image_url(raw_url: str) -> str:
    u = str(raw_url or "").strip()
    if not u or len(u) > 4096:
        raise ValueError("无效的图片地址")
    parsed = urlsplit(u)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("仅支持公开 http(s) 图片地址")
    if is_private_url(u):
        raise ValueError("禁止请求本机或内网地址")
    host = parsed.hostname.lower().strip(".")
    # http:// 仅对常见公开图片 CDN 自动升级或放行；升级 xhscdn/rednotecdn/hdslb/douyinpic 到 https
    if parsed.scheme == "http" and host.endswith((".xhscdn.com", ".rednotecdn.com", ".hdslb.com")):
        u = "https://" + u[len("http://"):]
    return u


def _is_forbidden_resolved_ip(addr: str) -> bool:
    """检查域名解析出的 IP 是否属于本机回环、局域网私网或链路本地地址；放行 Clash/Mihomo 等 TUN Fake-IP (198.18.0.0/15, fc00::/18)。"""
    import ipaddress

    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        # 放行 RFC 2544 (198.18.0.0/15)：桌面端代理软件 TUN Fake-IP 虚拟 DNS 标准网段
        if ip in ipaddress.IPv4Network("198.18.0.0/15"):
            return False
        if (
            ip in ipaddress.IPv4Network("10.0.0.0/8")
            or ip in ipaddress.IPv4Network("172.16.0.0/12")
            or ip in ipaddress.IPv4Network("192.168.0.0/16")
            or ip in ipaddress.IPv4Network("100.64.0.0/10")
            or ip in ipaddress.IPv4Network("0.0.0.0/8")
        ):
            return True
        return not ip.is_global
    # IPv6: 放行 TUN Fake-IP 常用段 fc00::/18 (如 fdfe:dcba:9876::/48)，拦截其余 ULA/非全局地址
    if ip in ipaddress.IPv6Network("fc00::/18"):
        return False
    return not ip.is_global


def _check_direct_dns_public(hostname: str) -> None:
    """直连拉取前校验域名解析结果不含回环/局域网私网 IP，防范 DNS 重绑定 SSRF，同时兼容 TUN Fake-IP。"""
    infos = socket.getaddrinfo(hostname, None)
    for item in infos:
        addr = item[4][0]
        if _is_forbidden_resolved_ip(addr):
            raise ValueError(f"目标域名解析指向受限内网地址 ({addr})，已拒绝")


def _sniff_image_ext(first_bytes: bytes, content_type: str) -> str:
    if first_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if first_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if first_bytes.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(first_bytes) >= 12 and first_bytes[:4] == b"RIFF" and first_bytes[8:12] == b"WEBP":
        return ".webp"
    if len(first_bytes) >= 12 and first_bytes[4:12] in (b"ftypavif", b"ftypavis"):
        return ".avif"
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    return _CTYPE_TO_EXT.get(ctype, ".jpg")


def _candidate_proxies_for_thumb(platform: str, explicit_proxy: str = "") -> tuple[list[str], str]:
    ua_str = getattr(getattr(app_runtime.cfg, "engine", None), "user_agent", "") or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    )
    cands: list[str] = []

    def _add(val: str) -> None:
        v = str(val or "").strip()
        if v and v not in cands:
            cands.append(v)

    _add(explicit_proxy)
    overseas = platform in OVERSEAS_PLATFORMS
    route_dl = bool(getattr(getattr(app_runtime.cfg, "engine", None), "route_download_via_proxy", True))

    if overseas or route_dl:
        try:
            with get_session() as s:
                acc_rows = s.exec(
                    select(DouyinAccount)
                    .where(DouyinAccount.platform == platform)
                    .order_by(DouyinAccount.id.desc())
                ).all()
                for acc in acc_rows:
                    if acc.ua:
                        ua_str = acc.ua
                        break
                for acc in acc_rows:
                    if (
                        acc.status == "active"
                        and acc.proxy
                        and acc.proxy_status not in {"bad", "auth_error", "blocked", "disabled"}
                    ):
                        _add(acc.proxy)
                if overseas:
                    _add(pick_ephemeral_proxy(s, app_runtime.cfg, platform=platform))
                    for p_url in _pool_urls(s, app_runtime.cfg, platform=platform):
                        _add(p_url)
                else:
                    # 国内平台仅当池中显式为该国内平台指定了专属代理节点时才纳入，避免误用全局海外节点拉取国内 CDN
                    from app.models import ProxyPoolEntry
                    dom_pool = s.exec(
                        select(ProxyPoolEntry).where(ProxyPoolEntry.enabled == True)  # noqa: E712
                    ).all()
                    for row in dom_pool:
                        if row.status in {"bad", "auth_error", "blocked", "disabled"}:
                            continue
                        try:
                            import json as _json
                            pfs = _json.loads(row.platforms or "[]")
                        except Exception:
                            pfs = []
                        if isinstance(pfs, list) and platform in pfs:
                            _add(row.url)
                if overseas and not cands:
                    other_overseas = s.exec(
                        select(DouyinAccount)
                        .where(DouyinAccount.platform.in_(OVERSEAS_PLATFORMS))
                        .order_by(DouyinAccount.id.desc())
                    ).all()
                    for acc in other_overseas:
                        if (
                            acc.status == "active"
                            and acc.proxy
                            and acc.proxy_status not in {"bad", "auth_error", "blocked", "disabled"}
                        ):
                            _add(acc.proxy)
        except Exception:
            pass
        if overseas and app_runtime.cfg is not None:
            for p_raw in (app_runtime.cfg.proxies or []):
                _add(p_raw)

    if not cands:
        return [""], ua_str
    if "" not in cands:
        return [*cands[:2], ""], ua_str
    return cands[:3], ua_str


async def ensure_cached_thumb(
    raw_url: str,
    *,
    platform: str = "",
    proxy: str = "",
) -> Path:
    """确保远程封面/头像已下载到本地磁盘缓存目录，返回本地 Path（已存在则直接返回，永不重复拉取）。"""
    clean_url = _validate_public_image_url(raw_url)
    existing = cached_thumb_file(raw_url) or cached_thumb_file(clean_url)
    if existing is not None:
        return existing

    key = _url_cache_key(raw_url)
    lock = _fetch_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _fetch_locks[key] = lock

    async with lock:
        existing = cached_thumb_file(raw_url) or cached_thumb_file(clean_url)
        if existing is not None:
            return existing

        from curl_cffi.requests import AsyncSession

        pf = detect_cdn_platform(clean_url, platform)
        referer = _PLATFORM_REFERERS.get(pf, "https://www.douyin.com/")
        attempts, ua_str = _candidate_proxies_for_thumb(pf, explicit_proxy=proxy)
        headers = {
            "User-Agent": ua_str,
            "Referer": referer,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        imp = impersonate_for_ua(ua_str)
        mirrors, signed_expired = xhs_unsigned_cdn_urls(clean_url, pf)
        url_candidates = [*mirrors, clean_url] if (mirrors and signed_expired) else [clean_url, *mirrors]
        last_err: Exception | None = None

        for px in attempts:
            for target_idx, candidate_url in enumerate(url_candidates):
                has_more_urls = target_idx < len(url_candidates) - 1
                try:
                    req_headers = (
                        {k: v for k, v in headers.items() if k != "Referer"}
                        if candidate_url != clean_url
                        else headers
                    )
                    async with AsyncSession(
                        impersonate=imp,
                        trust_env=False,
                        timeout=12.0,
                        headers=req_headers,
                        proxies=curl_proxies(px),
                    ) as http:
                        cur_url = candidate_url
                        resp = None
                        for _hop in range(4):
                            cur_host = (urlsplit(cur_url).hostname or "").strip()
                            if not px and cur_host:
                                await asyncio.to_thread(_check_direct_dns_public, cur_host)
                            resp = await http.get(cur_url, allow_redirects=False, stream=True)
                            if resp.status_code in {301, 302, 303, 307, 308}:
                                loc = (resp.headers.get("location") or "").strip()
                                await resp.aclose()
                                resp = None
                                if not loc or _hop >= 3:
                                    raise HTTPException(502, "上游图片重定向异常")
                                from urllib.parse import urljoin
                                cur_url = _validate_public_image_url(urljoin(cur_url, loc))
                                continue
                            break
                        assert resp is not None
                        try:
                            if resp.status_code != 200:
                                raise HTTPException(
                                    resp.status_code if 400 <= resp.status_code < 600 else 502,
                                    f"上游图片返回 HTTP {resp.status_code}",
                                )
                            ctype = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                            chunks: list[bytes] = []
                            total = 0
                            async for chunk in resp.aiter_content():
                                if not chunk:
                                    continue
                                total += len(chunk)
                                if total > 10 * 1024 * 1024:
                                    raise HTTPException(413, "图片体积超出缓存上限")
                                chunks.append(chunk)
                            data = b"".join(chunks)
                            if not data:
                                raise HTTPException(502, "上游返回空图片")
                            ext = _sniff_image_ext(data[:16], ctype)
                            if not ctype.startswith("image/") and ext == ".jpg" and not data.startswith(b"\xff\xd8\xff"):
                                raise HTTPException(415, "上游响应不是有效图片")
                            bucket = _thumbs_root() / key[:2]
                            bucket.mkdir(parents=True, exist_ok=True)
                            dest = bucket / f"{key}{ext}"
                            tmp = bucket / f"{key}.{id(resp)}.part"
                            tmp.write_bytes(data)
                            tmp.replace(dest)
                            return dest
                        finally:
                            await resp.aclose()
                except HTTPException as hexc:
                    last_err = hexc
                    # 若当前签名 URL 返回 403/404，继续尝试无签名永久 CDN 镜像
                    if has_more_urls and hexc.status_code in {403, 404, 410, 502, 503, 504}:
                        continue
                    if hexc.status_code in {404, 410, 413, 415}:
                        raise
                    break
                except ValueError as vexc:
                    raise HTTPException(400, str(vexc)) from vexc
                except Exception as exc:
                    last_err = exc
                    if has_more_urls:
                        continue
                    break

        if isinstance(last_err, HTTPException):
            raise last_err
        err_name = type(last_err).__name__ if last_err else "UpstreamError"
        raise HTTPException(502, f"封面图片缓存拉取失败: {err_name}") from last_err


def file_thumb_response(path: Path) -> FileResponse:
    media_type = _EXT_TO_CTYPE.get(path.suffix.lower(), "image/jpeg")
    return FileResponse(
        path,
        media_type=media_type,
        content_disposition_type="inline",
        headers=IMMUTABLE_CACHE_HEADERS,
    )


async def serve_cached_thumb(raw_url: str, *, platform: str = "", proxy: str = "") -> FileResponse:
    try:
        path = await ensure_cached_thumb(raw_url, platform=platform, proxy=proxy)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return file_thumb_response(path)


async def _prewarm_one(url: str, platform: str, proxy: str) -> None:
    try:
        async with _prewarm_sem:
            await ensure_cached_thumb(url, platform=platform, proxy=proxy)
    except Exception:
        pass
    finally:
        _prewarm_inflight.discard(url)


def schedule_thumb_prewarm(
    urls: Iterable[str],
    *,
    platform: str = "",
    proxy: str = "",
) -> None:
    """在采集/监控/作品同步入库瞬间异步预热封面与头像到本地磁盘，打开页面时零等待直接读盘。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for raw in urls:
        u = str(raw or "").strip()
        if not u or u.startswith("/api/") or not u.lower().startswith(("http://", "https://")):
            continue
        if u in _prewarm_inflight or cached_thumb_file(u) is not None:
            continue
        if len(_prewarm_inflight) >= 120:
            break
        _prewarm_inflight.add(u)
        loop.create_task(_prewarm_one(u, platform, proxy))
