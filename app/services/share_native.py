"""分享链接原生元数据归一化与抖音无水印直连/浏览器回退解析。"""
from __future__ import annotations

from pathlib import Path
import json
import re

from app import runtime as app_runtime
from app.browser.douyin_detail import fetch_douyin_video_detail
from app.db import get_session
from app.engine import Downloader
from app.engine.share_downloader import ShareDownloadError, ShareDownloadFallback
from app.models import DouyinAccount
from app.platforms.douyin import DouyinClient, cookie_from_state as douyin_cookie_from_state, parse_aweme, resolve_aweme_id, safe_title
from app.risk import OperationKind, RiskCategory, classify_platform_error
from app.services import account_profiles as account_profiles_service
from app.services import login_sessions as login_sessions_service
from app.services import share_downloads as share_downloads_service
from app.settings import get_setting
from app.transport_matrix import douyin_client_environment, resolve_transport


def _native_aweme_metadata(aweme, source_url: str) -> dict:
    platform = str(getattr(aweme, "platform", "") or "douyin")
    platform_name = "Xhs" if platform == "xhs" else platform.title()
    return {
        "id": aweme.aweme_id,
        "title": aweme.desc or aweme.aweme_id,
        "description": aweme.desc,
        "uploader": aweme.author_name,
        "duration": aweme.duration,
        "timestamp": aweme.create_time,
        "like_count": aweme.like_count,
        "comment_count": aweme.comment_count,
        "thumbnail": aweme.cover,
        "webpage_url": source_url,
        "original_url": source_url,
        "extractor": f"creatorhub:{platform}",
        "extractor_key": f"CreatorHub{platform_name}",
        "ext": "jpg" if aweme.media_type == "images" else "mp4",
        "format": aweme.quality_label,
        "platform": platform,
        "media_type": aweme.media_type,
        "media_count": len(aweme.medias),
    }


def _douyin_share_terminal_error(error) -> bool:
    category, _ = classify_platform_error(error)
    code = str(error or "")
    signature_rejected = code.startswith("api_signature:") and code != "api_signature:missing_uifid"
    return (category in {RiskCategory.AUTH, RiskCategory.RISK}
            or signature_rejected or code.startswith(("verification_required", "aweme_unavailable")))


def _douyin_share_read_error(error, aweme_id: str, *, allow_fallback: bool,
                             diagnostics: dict | None = None):
    """Do not describe an empty response as an expired account."""
    def fail(message: str, error_type=ShareDownloadError):
        status = (diagnostics or {}).get("http_status")
        suffix = f"（HTTP {status}）" if isinstance(status, int) and status != 200 else ""
        exc = error_type(message + suffix)
        exc.diagnostics = dict(diagnostics or {})
        raise exc

    category, _ = classify_platform_error(error)
    code = str(error or "no_detail_response")
    if code.startswith("api_signature:"):
        reasons = {
            "missing_uifid": "账号会话中缺少 API 请求所需的 UIFID，请在账号浏览器打开作品以同步会话",
            "uifid_not_found": "API 请求的设备标识未被接受",
            "signature_not_found": "API 请求缺少网页签名",
            "sign_invalid": "API 请求签名校验失败",
            "sign_expired": "API 请求签名已过期",
        }
        fail(reasons.get(code.split(":", 1)[1], "API 请求签名参数不完整") + "；这不代表账号登录失效")
    if code.startswith("risk:request_denied:"):
        fail("抖音拒绝了本次 API 请求；响应尚未明确是会话、签名还是请求限制，请查看诊断信息")
    if code.startswith("risk:rate_limited:"):
        fail("抖音暂时限制请求频率，请等待冷却结束后重试")
    if category == RiskCategory.AUTH:
        fail("抖音页面或接口明确要求登录，请对所选账号重新登录后再下载")
    if category == RiskCategory.RISK or code.startswith("verification_required"):
        fail("抖音要求安全验证或限制当前请求；请在该账号浏览器中完成验证后重试")
    if code.startswith("aweme_unavailable"):
        fail("抖音作品不存在、已删除或当前账号没有访问权限")
    if code == "browser_closed":
        fail("作品读取期间浏览器被关闭，请重试并保持窗口打开")
    reasons = {
        "empty_body": "接口返回空响应（empty_body）",
        "empty_response": "接口未返回数据（empty_response）",
        "missing_aweme_detail": "接口未返回目标作品详情",
        "no_detail_response": "页面未返回目标作品详情",
        "invalid_json": "接口未返回有效 JSON",
        "detail_body_unavailable": "页面跳转时详情响应读取失败",
        "browser_unavailable": "账号浏览器未就绪",
        "missing_media": "详情中没有可下载的视频或图片",
        "no_cookie": "账号没有保存可复用的会话 Cookie",
    }
    if code.startswith("network:"):
        reason = "网络请求失败或超时"
    elif re.fullmatch(r"http_\d{3}|api_rejected:status_code=\d+", code):
        reason = code
    else:
        reason = reasons.get(code, "作品详情读取未成功")
    error_type = ShareDownloadFallback if allow_fallback else ShareDownloadError
    fail(f"已识别抖音作品 {aweme_id}，但{reason}", error_type)


async def _douyin_native_share(
    source_url: str,
    *,
    account_id: int | None,
    output_root: Path,
    quality: str,
    should_download: bool,
    save_metadata: bool,
    save_thumbnail: bool,
    proxy: str,
    user_agent: str,
) -> dict | None:
    """按读取模式复用所选账号，解析视频、图文和短链。

    返回 None 表示它不是可解析的抖音单作品链接，应继续走通用提取器。
    """
    if account_id is None:
        return None
    with get_session() as s:
        account = s.get(DouyinAccount, account_id)
        if not account or account.platform != "douyin":
            return None
        if account.status == "invalid":
            raise ShareDownloadError("所选抖音账号登录态已失效，请重新登录后再下载")
        state = account.storage_state or account.creator_storage_state or ""
        raw_cookie = account.cookie or ""
        transport = resolve_transport(app_runtime.cfg, "douyin", "share_download", account)
        mode = transport["effective_mode"]
    lease = app_runtime.open_browsers.get(account_id)
    if lease is not None and getattr(lease, "active", True):
        raise ShareDownloadError("请先关闭该账号手动打开的浏览器窗口，再执行链接下载")
    if app_runtime.engine is None:
        raise ShareDownloadError("账号读取引擎未就绪，请稍后重试")

    cookie = douyin_cookie_from_state(state) or raw_cookie
    from app.platforms.douyin.web_session import context_key, validated_detail_context
    api_context = validated_detail_context(
        get_setting(context_key(account_id), ""), account, state, user_agent)
    aweme_id = ""
    used_browser = False
    download_ua = user_agent
    api_diagnostics = {}

    async def _read_detail():
        nonlocal aweme_id, used_browser, download_ua, api_diagnostics
        from app.browser.manager import normalize_proxy
        if account is not None and normalize_proxy(proxy) != normalize_proxy(account.proxy or ""):
            return {}, "browser_proxy_mismatch"
        aweme_id = await resolve_aweme_id(source_url, user_agent, proxy=proxy) or ""
        if not aweme_id:
            return {}, "unresolved_aweme_id"
        raw, error = None, "no_cookie"
        # Hybrid defaults to direct API first. The API reuses the account's
        # calibrated session when available; only inconclusive failures reach
        # the browser. Explicit authentication/risk denials still stop here.
        channels = ("browser", "api") if mode == "hybrid" and transport.get(
            "hybrid_order") == "browser_first" else (
                ("api", "browser") if mode == "hybrid" else (mode,))
        for channel in channels:
            if channel == "api":
                if not cookie:
                    continue
                client = DouyinClient(cookie, user_agent,
                                      timeout=app_runtime.cfg.engine.request_timeout_seconds,
                                      proxy=proxy, storage_state=state,
                                      detail_context=api_context,
                                      **douyin_client_environment(account))
                try:
                    async with client.session_scope():
                        raw = await client.fetch_video_detail(aweme_id)
                    api_diagnostics = dict(getattr(client, "last_response", {}) or {})
                    error = client.last_error or ("" if raw else "empty_response")
                except Exception as exc:
                    raw, error = None, f"network:{type(exc).__name__}"
            elif channel == "browser":
                if app_runtime.browser is None:
                    raw, error = None, "browser_unavailable"
                    continue
                identity = app_runtime.browser.identity_for(account)
                from app.browser.manager import normalize_proxy
                if normalize_proxy(proxy) != normalize_proxy(identity.proxy):
                    return {}, "browser_proxy_mismatch"
                raw, error = await fetch_douyin_video_detail(
                    app_runtime.browser, identity, aweme_id, source_url=source_url)
            else:
                return {}, "transport_unavailable"
            if raw and parse_aweme(raw, "highest"):
                used_browser = channel == "browser"
                if used_browser:
                    download_ua = account_profiles_service._direct_request_ua(identity)
                else:
                    export_state = getattr(client, "export_storage_state", None)
                    if callable(export_state):
                        login_sessions_service._persist_douyin_api_cookies(account_id, state, export_state())
                return raw, ""
            if _douyin_share_terminal_error(error):
                return {}, error
        return {}, error or "missing_media"

    raw, outcome = await account_profiles_service._run_account_read(
        account_id, OperationKind.READ_LIGHT, f"share-douyin:{account_id}",
        _read_detail, empty_result={}, interactive_read=True)
    if isinstance(outcome, dict):
        raise ShareDownloadError(f"抖音账号读取已延后：{outcome.get('reason') or '稍后重试'}")
    if outcome == "browser_proxy_mismatch":
        raise ShareDownloadError("浏览器读取需使用账号已绑定的代理，请清除下载页的临时代理设置后重试")
    if not aweme_id and outcome == "unresolved_aweme_id":
        if mode == "browser":
            raise ShareDownloadError("未从该链接识别到抖音作品 ID，请复制具体作品的分享链接")
        return None
    if outcome:
        _douyin_share_read_error(outcome, aweme_id, allow_fallback=mode == "hybrid",
                                 diagnostics=api_diagnostics)
    if not raw:
        _douyin_share_read_error("missing_aweme_detail", aweme_id, allow_fallback=mode == "hybrid",
                                 diagnostics=api_diagnostics)
    aweme = parse_aweme(raw, quality if quality != "audio" else "highest")
    if not aweme:
        _douyin_share_read_error("missing_media", aweme_id, allow_fallback=mode == "hybrid",
                                 diagnostics=api_diagnostics)

    # 原生直链下载器不做音频转码；仅音频请求继续交给 yt-dlp/ffmpeg。
    if quality == "audio" and aweme.media_type == "video":
        return None

    metadata = _native_aweme_metadata(aweme, source_url)
    metadata["read_transport"] = "browser" if used_browser else "api"
    if not used_browser and api_diagnostics:
        metadata["api_diagnostics"] = api_diagnostics
    warnings = (["已使用所选账号的浏览器读取作品详情"]
                if used_browser and mode == "hybrid" else [])
    if not should_download:
        return {
            "ok": True,
            "url": source_url,
            "metadata": metadata,
            "warnings": warnings,
        }

    downloader = Downloader(
        str(output_root),
        download_ua,
        timeout=max(30.0, app_runtime.cfg.engine.request_timeout_seconds),
    )
    ok, _local_path, error = await downloader.download_aweme(
        aweme, base_dir=str(output_root), proxy=proxy
    )
    if not ok:
        raise ShareDownloadError(error or "抖音媒体下载失败")

    target_dir = output_root / safe_title(aweme.author_name or "unknown")
    title = safe_title(aweme.desc) or aweme.aweme_id
    if save_metadata:
        info_path = target_dir / f"{aweme.aweme_id}_{title}.info.json"
        payload = {
            **metadata,
            "media": [
                {"url": media.url, "kind": media.kind, "ext": media.ext,
                 "index": media.index}
                for media in aweme.medias
            ],
            "raw": raw,
        }
        info_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    # 视频封面单独保存；图文作品的图片本身已经全部下载。
    if save_thumbnail and aweme.media_type == "video" and aweme.cover:
        from curl_cffi.requests import AsyncSession
        from app.netfp import curl_proxy_url, impersonate_for_ua

        cover_path = target_dir / f"{aweme.aweme_id}_{title}.cover.jpg"
        headers = {"User-Agent": download_ua, "Referer": "https://www.douyin.com/"}
        try:
            async with AsyncSession(
                timeout=max(30.0, app_runtime.cfg.engine.request_timeout_seconds),
                allow_redirects=True,
                headers=headers,
                proxy=curl_proxy_url(proxy) or None,
                impersonate=impersonate_for_ua(download_ua),
                trust_env=False,
            ) as http:
                await downloader._download_one(http, aweme.cover, cover_path)
        except Exception:
            pass

    files = []
    for path in sorted(target_dir.glob(f"{aweme.aweme_id}_*")):
        if not path.is_file() or path.suffix.lower() in {".part", ".ytdl"}:
            continue
        files.append({
            "name": path.name,
            "path": str(path.resolve()),
            "relative_path": path.relative_to(output_root).as_posix(),
            "size": path.stat().st_size,
            "role": share_downloads_service._share_file_role(path),
        })
    if not any(item["role"] == "media" for item in files):
        raise ShareDownloadError("抖音作品解析成功，但本地没有生成媒体文件")
    return {
        "ok": True,
        "job_id": f"douyin_{aweme.aweme_id}",
        "url": source_url,
        "output_dir": str(target_dir.resolve()),
        "metadata": metadata,
        "files": files,
        "progress": {"status": "finished"},
        "warnings": warnings,
    }
