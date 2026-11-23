"""小红书、海外小红书、Instagram 与 X (Twitter) 原生无水印分享解析。"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit
import json

from sqlmodel import select

from app import runtime as app_runtime
from app.browser import fetch_rednote_note_detail, fetch_twitter_tweet_detail, fetch_xhs_note_detail
from app.db import get_session
from app.engine import Downloader
from app.engine.share_downloader import ShareDownloadError, ShareDownloadFallback
from app.models import DouyinAccount
from app.platforms.douyin import safe_title
from app.platforms.instagram import InstagramClient
from app.platforms.rednote import RednoteApiClient, parse_rednote_detail, cookie_str_from_state as rednote_cookie_str_from_state, has_a1 as rednote_has_a1, resolve_rednote_note
from app.platforms.twitter import TwitterClient, parse_twitter_aweme
from app.platforms.xhs import resolve_note as xhs_resolve_note
from app.risk import OperationKind
from app.services import account_profiles as account_profiles_service
from app.services import share_downloads as share_downloads_service
from app.services import share_native as share_native_service


async def _instagram_native_share(
    source_url: str,
    *,
    account_id: int | None,
    output_root: Path,
    quality: str = "highest",
    should_download: bool,
    save_metadata: bool,
    save_thumbnail: bool,
    proxy: str,
    user_agent: str,
) -> dict | None:
    """使用 InstagramClient 直连解析单图 / 轮播 / Reels 并下载无水印原片。"""
    state_text = ""
    acc_proxy = proxy
    acc_ua = user_agent
    acc_seed = ""
    acc_tz = "America/New_York"
    if account_id:
        with get_session() as session:
            acc = session.get(DouyinAccount, account_id)
            if acc is not None and acc.platform == "instagram" and acc.status == "active":
                state_text = acc.storage_state or acc.creator_storage_state or acc.cookie or ""
                acc_proxy = proxy or (acc.proxy or "")
                acc_ua = acc.ua or acc_ua
                acc_seed = acc.fp_seed or acc.profile_dir or ""
                acc_tz = acc.timezone_id or "America/New_York"

    if not state_text:
        raise ShareDownloadFallback("未找到已登录的 Instagram 账号，回退通用下载器")

    icli = InstagramClient(
        state_text,
        acc_ua,
        proxy=acc_proxy,
        timeout=app_runtime.cfg.engine.request_timeout_seconds,
        account_seed=acc_seed,
        timezone_id=acc_tz,
    )
    async with icli.session_scope():
        aweme, raw_item, err = await icli.fetch_media_info(source_url)
    if aweme is None or not aweme.medias:
        if str(err or "").startswith(("logged_out:", "verification_required:")):
            raise ShareDownloadError(f"Instagram 账号登录态异常: {err}")
        raise ShareDownloadFallback(err or "Instagram 媒体详情为空")

    # 仅音频提取交给通用提取器 (yt-dlp + ffmpeg)
    if quality == "audio" and aweme.media_type == "video":
        return None

    metadata = share_native_service._native_aweme_metadata(aweme, aweme.source_url or source_url)
    metadata["uploader"] = aweme.author_name or aweme.author_unique_id
    metadata["uploader_id"] = aweme.author_unique_id
    if not should_download:
        return {
            "ok": True,
            "job_id": f"instagram_{aweme.aweme_id}",
            "url": source_url,
            "output_dir": str(output_root.resolve()),
            "metadata": metadata,
            "files": [],
            "progress": {"status": "inspected"},
            "warnings": [],
        }

    target_root = output_root / "instagram"
    dl_ua = acc_ua or app_runtime.cfg.engine.user_agent
    dl_timeout = max(30.0, float(app_runtime.cfg.engine.download_timeout_seconds))
    downloader = Downloader(
        str(target_root),
        dl_ua,
        timeout=dl_timeout,
    )
    ok, saved_path, dl_err = await downloader.download_aweme(
        aweme,
        base_dir=str(target_root),
        proxy=acc_proxy,
        user_agent=dl_ua,
        referer="https://www.instagram.com/",
    )
    if not ok:
        raise ShareDownloadError(f"Instagram 媒体下载失败: {dl_err}")

    saved = Path(saved_path)
    target_dir = saved if saved.is_dir() else saved.parent
    title = safe_title(aweme.desc) if str(aweme.desc or "").strip() else aweme.aweme_id
    if save_metadata:
        info_path = target_dir / f"{aweme.aweme_id}_{title}.info.json"
        info_path.write_text(
            json.dumps(
                {
                    **metadata,
                    "media": [
                        {"url": m.url, "kind": m.kind, "ext": m.ext, "index": m.index}
                        for m in aweme.medias
                    ],
                    "raw": raw_item,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
    if save_thumbnail and aweme.media_type == "video" and aweme.cover:
        from curl_cffi.requests import AsyncSession
        from app.netfp import curl_proxy_url, impersonate_for_ua

        cover_path = target_dir / f"{aweme.aweme_id}_{title}.cover.jpg"
        headers = {"User-Agent": dl_ua, "Referer": "https://www.instagram.com/"}
        try:
            async with AsyncSession(
                timeout=dl_timeout,
                allow_redirects=True,
                headers=headers,
                proxy=curl_proxy_url(acc_proxy) or None,
                impersonate=impersonate_for_ua(dl_ua),
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
        raise ShareDownloadError("Instagram 作品解析成功，但本地未生成媒体文件")
    return {
        "ok": True,
        "job_id": f"instagram_{aweme.aweme_id}",
        "url": source_url,
        "output_dir": str(target_dir.resolve()),
        "metadata": metadata,
        "files": files,
        "progress": {"status": "finished"},
        "warnings": [],
    }


async def _twitter_native_share(
    source_url: str,
    *,
    account_id: int | None,
    output_root: Path,
    quality: str = "highest",
    should_download: bool,
    save_metadata: bool,
    save_thumbnail: bool,
    proxy: str,
    user_agent: str,
) -> dict | None:
    """使用 TwitterClient Syndication 直连（及账号浏览器回退）解析推文视频/多图原图并下载。"""
    state_text = ""
    acc_proxy = proxy
    acc_ua = user_agent
    identity = None
    if account_id:
        with get_session() as session:
            acc = session.get(DouyinAccount, account_id)
            if acc is not None and acc.platform == "twitter" and acc.status == "active":
                state_text = acc.storage_state or acc.creator_storage_state or acc.cookie or ""
                acc_proxy = proxy or (acc.proxy or "")
                acc_ua = acc.ua or acc_ua
                if app_runtime.browser is not None:
                    identity = app_runtime.browser.identity_for(acc)

    tcli = TwitterClient(
        state_text,
        acc_ua,
        proxy=acc_proxy,
        timeout=app_runtime.cfg.engine.request_timeout_seconds,
    )
    async with tcli.session_scope():
        aweme, raw_item, err = await tcli.fetch_tweet_syndication(source_url)

    if (aweme is None or not aweme.medias) and app_runtime.browser is not None and identity is not None and state_text:
        node, b_err = await fetch_twitter_tweet_detail(
            app_runtime.browser, identity, source_url, block_media=app_runtime.cfg.engine.block_media_resources
        )
        if node:
            aweme = parse_twitter_aweme(node)
            raw_item = node
            err = ""
        elif b_err:
            err = err or b_err

    if aweme is None or not aweme.medias:
        if str(err or "").startswith(("logged_out:", "verification_required:")):
            raise ShareDownloadError(f"X (Twitter) 账号登录态异常: {err}")
        raise ShareDownloadFallback(err or "Twitter 推文未提取到媒体直链，回退通用下载器")

    if quality == "audio" and aweme.media_type == "video":
        return None

    metadata = share_native_service._native_aweme_metadata(aweme, aweme.source_url or source_url)
    metadata["uploader"] = aweme.author_name or aweme.author_unique_id
    metadata["uploader_id"] = aweme.author_unique_id
    if not should_download:
        return {
            "ok": True,
            "job_id": f"twitter_{aweme.aweme_id}",
            "url": source_url,
            "output_dir": str(output_root.resolve()),
            "metadata": metadata,
            "files": [],
            "progress": {"status": "inspected"},
            "warnings": [],
        }

    target_root = output_root / "twitter"
    dl_ua = acc_ua or app_runtime.cfg.engine.user_agent
    dl_timeout = max(30.0, float(app_runtime.cfg.engine.download_timeout_seconds))
    downloader = Downloader(str(target_root), dl_ua, timeout=dl_timeout)
    ok, saved_path, dl_err = await downloader.download_aweme(
        aweme,
        base_dir=str(target_root),
        proxy=acc_proxy,
        user_agent=dl_ua,
        referer="https://x.com/",
    )
    if not ok:
        raise ShareDownloadError(f"X (Twitter) 媒体下载失败: {dl_err}")

    saved = Path(saved_path)
    target_dir = saved if saved.is_dir() else saved.parent
    title = safe_title(aweme.desc) if str(aweme.desc or "").strip() else aweme.aweme_id
    if save_metadata:
        info_path = target_dir / f"{aweme.aweme_id}_{title}.info.json"
        info_path.write_text(
            json.dumps(
                {
                    **metadata,
                    "media": [
                        {"url": m.url, "kind": m.kind, "ext": m.ext, "index": m.index}
                        for m in aweme.medias
                    ],
                    "raw": raw_item,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
    if save_thumbnail and aweme.media_type == "video" and aweme.cover:
        from curl_cffi.requests import AsyncSession
        from app.netfp import curl_proxy_url, impersonate_for_ua

        cover_path = target_dir / f"{aweme.aweme_id}_{title}.cover.jpg"
        headers = {"User-Agent": dl_ua, "Referer": "https://x.com/"}
        try:
            async with AsyncSession(
                timeout=dl_timeout,
                allow_redirects=True,
                headers=headers,
                proxy=curl_proxy_url(acc_proxy) or None,
                impersonate=impersonate_for_ua(dl_ua),
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
        raise ShareDownloadError("Twitter 推文解析成功，但本地未生成媒体文件")
    return {
        "ok": True,
        "job_id": f"twitter_{aweme.aweme_id}",
        "url": source_url,
        "output_dir": str(target_dir.resolve()),
        "metadata": metadata,
        "files": files,
        "progress": {"status": "finished"},
        "warnings": [],
    }


async def _xhs_native_share(
    source_url: str,
    *,
    account_id: int | None,
    output_root: Path,
    quality: str,
    should_download: bool,
    save_metadata: bool,
    save_thumbnail: bool,
    proxy: str,
) -> dict | None:
    """Read XHS note metadata in the selected account browser, then fetch CDN media."""
    if account_id is None or not account_profiles_service._xhs_browser_reads_enabled():
        return None
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if not account or account.platform != "xhs":
            return None
        identity = app_runtime.browser.identity_for(account)

    dl_proxy = proxy or (identity.proxy or "")

    async def _read_note():
        final_url = source_url
        host = (urlsplit(source_url).hostname or "").lower()
        if host == "xhslink.com" or host.endswith(".xhslink.com"):
            try:
                async with app_runtime.browser.visible_page(
                        identity, url=source_url) as page:
                    await page.wait_for_timeout(800)
                    final_url = str(page.url or source_url)
            except Exception as exc:
                return None, f"小红书短链解析失败: {exc}"
        ref = await xhs_resolve_note(
            final_url,
            account_profiles_service._direct_request_ua(identity),
            proxy=dl_proxy,
        )
        if ref is None:
            return None, "没有从链接识别到小红书笔记 ID"
        card, read_error = await fetch_xhs_note_detail(
            app_runtime.browser,
            identity,
            ref.note_id,
            xsec_token=ref.xsec_token,
            xsec_source=ref.xsec_source or "pc_feed",
            block_media=app_runtime.cfg.engine.block_media_resources,
        )
        return {
            "card": card or {},
            "note_id": ref.note_id,
            "final_url": final_url,
        }, read_error

    payload, outcome = await account_profiles_service._run_account_read(
        account_id,
        OperationKind.READ_HEAVY,
        f"share-xhs:{account_id}",
        _read_note,
        empty_result={"card": {}, "note_id": "", "final_url": source_url},
    )
    if isinstance(outcome, dict):
        raise ShareDownloadError(
            f"小红书账号读取正在冷却: {outcome.get('reason') or '稍后重试'}")
    if outcome:
        raise ShareDownloadError(f"小红书笔记读取失败: {outcome}")

    from app.platforms.xhs import parse_note_detail as parse_xhs_note_detail
    note_id = str(payload.get("note_id") or "")
    card = payload.get("card") or {}
    aweme = parse_xhs_note_detail(card, {"note_id": note_id})
    if not aweme or not aweme.medias:
        raise ShareDownloadError("小红书笔记已打开，但没有取得可下载的图片或视频")

    # 不把账号 Cookie 回退给 yt-dlp 直连页面；这样才不会在同一登录态下
    # 突然切换 TLS/HTTP2 指纹。
    if quality == "audio" and aweme.media_type == "video":
        raise ShareDownloadError(
            "小红书账号浏览器模式暂不执行音频提取，请选择视频画质下载"
        )

    actual_ua = account_profiles_service._direct_request_ua(identity)
    metadata = share_native_service._native_aweme_metadata(
        aweme, str(payload.get("final_url") or source_url))
    if not should_download:
        return {
            "ok": True,
            "url": source_url,
            "metadata": metadata,
            "warnings": [],
        }

    downloader = Downloader(
        str(output_root), actual_ua,
        timeout=max(30.0, app_runtime.cfg.engine.download_timeout_seconds),
    )
    ok, _local_path, error = await downloader.download_aweme(
        aweme, base_dir=str(output_root), proxy=dl_proxy)
    if not ok:
        raise ShareDownloadError(error or "小红书媒体下载失败")

    target_dir = output_root / safe_title(aweme.author_name or "unknown")
    title = safe_title(aweme.desc) or aweme.aweme_id
    if save_metadata:
        info_path = target_dir / f"{aweme.aweme_id}_{title}.info.json"
        info_path.write_text(
            json.dumps({
                **metadata,
                "media": [
                    {"url": media.url, "kind": media.kind,
                     "ext": media.ext, "index": media.index}
                    for media in aweme.medias
                ],
                "raw": card,
            }, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    if save_thumbnail and aweme.media_type == "video" and aweme.cover:
        from curl_cffi.requests import AsyncSession
        from app.netfp import curl_proxy_url, impersonate_for_ua
        cover_path = target_dir / f"{aweme.aweme_id}_{title}.cover.jpg"
        try:
            async with AsyncSession(
                    timeout=max(30.0, app_runtime.cfg.engine.download_timeout_seconds),
                    allow_redirects=True,
                    headers={
                        "User-Agent": actual_ua,
                        "Referer": "https://www.xiaohongshu.com/",
                    },
                    proxy=curl_proxy_url(identity.proxy) or None,
                    impersonate=impersonate_for_ua(actual_ua),
                    trust_env=False) as http:
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
        raise ShareDownloadError("小红书笔记解析成功，但本地没有生成媒体文件")
    return {
        "ok": True,
        "job_id": f"xhs_{aweme.aweme_id}",
        "url": source_url,
        "output_dir": str(target_dir.resolve()),
        "metadata": metadata,
        "files": files,
        "progress": {"status": "finished"},
        "warnings": [],
    }


async def _rednote_native_share(
    source_url: str,
    *,
    account_id: int | None,
    output_root: Path,
    quality: str,
    should_download: bool,
    save_metadata: bool,
    save_thumbnail: bool,
    proxy: str,
) -> dict | None:
    """Read Rednote (海外小红书) note metadata in the selected account session, then fetch CDN media."""
    if account_id is None:
        return None
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if not account or account.platform != "rednote":
            return None
        state = account.storage_state or account.creator_storage_state or ""
        identity = app_runtime.browser.identity_for(account) if app_runtime.browser is not None else None

    acc_proxy = proxy or (identity.proxy if identity is not None else (account.proxy or ""))

    actual_ua = (
        account_profiles_service._direct_request_ua(identity)
        if app_runtime.browser is not None and identity is not None
        else (account.ua or app_runtime.cfg.engine.user_agent)
    )
    rmode = str(getattr(app_runtime.cfg.engine, "rednote_read_mode", "hybrid") or "hybrid").lower()
    rn_cookie = rednote_cookie_str_from_state(state)

    async def _read_note():
        ref = await resolve_rednote_note(source_url, actual_ua, proxy=acc_proxy or "")
        if ref is None:
            return None, "没有从链接识别到海外小红书笔记 ID"
        if rmode in {"api", "hybrid"} and rn_cookie and rednote_has_a1(rn_cookie):
            rcli = RednoteApiClient(
                rn_cookie, actual_ua,
                timeout=app_runtime.cfg.engine.request_timeout_seconds, proxy=acc_proxy,
                storage_state=state,
            )
            try:
                async with rcli.session_scope():
                    card = await rcli.note_detail(
                        ref.note_id,
                        xsec_token=ref.xsec_token,
                        xsec_source=ref.xsec_source or "pc_feed",
                    )
                if card:
                    return {"card": card, "note_id": ref.note_id, "final_url": source_url}, ""
            except Exception as exc:
                if rmode == "api":
                    return None, str(exc)
        if app_runtime.browser is None or identity is None:
            return None, "浏览器读取不可用"
        card, read_error = await fetch_rednote_note_detail(
            app_runtime.browser,
            identity,
            ref.note_id,
            xsec_token=ref.xsec_token,
            xsec_source=ref.xsec_source or "pc_feed",
            block_media=app_runtime.cfg.engine.block_media_resources,
        )
        return {
            "card": card or {},
            "note_id": ref.note_id,
            "final_url": source_url,
        }, read_error

    payload, outcome = await account_profiles_service._run_account_read(
        account_id,
        OperationKind.READ_HEAVY,
        f"share-rednote:{account_id}",
        _read_note,
        empty_result={"card": {}, "note_id": "", "final_url": source_url},
    )
    if isinstance(outcome, dict):
        raise ShareDownloadError(
            f"海外小红书账号读取正在冷却: {outcome.get('reason') or '稍后重试'}")
    if outcome:
        raise ShareDownloadError(f"海外小红书笔记读取失败: {outcome}")

    note_id = str(payload.get("note_id") or "")
    card = payload.get("card") or {}
    aweme = parse_rednote_detail(card, {"note_id": note_id})
    if not aweme or not aweme.medias:
        raise ShareDownloadError("海外小红书笔记已打开，但没有取得可下载的图片或视频")
    if quality == "audio" and aweme.media_type == "video":
        raise ShareDownloadError("海外小红书模式暂不执行音频提取，请选择视频画质下载")

    metadata = share_native_service._native_aweme_metadata(
        aweme, str(payload.get("final_url") or source_url))
    if not should_download:
        return {
            "ok": True,
            "url": source_url,
            "metadata": metadata,
            "warnings": [],
        }

    downloader = Downloader(
        str(output_root), actual_ua,
        timeout=max(30.0, app_runtime.cfg.engine.download_timeout_seconds),
    )
    ok, _local_path, error = await downloader.download_aweme(
        aweme, base_dir=str(output_root), proxy=acc_proxy or "",
        referer="https://www.rednote.com/",
    )
    if not ok:
        raise ShareDownloadError(error or "海外小红书媒体下载失败")

    target_dir = output_root / safe_title(aweme.author_name or "unknown")
    title = safe_title(aweme.desc) or aweme.aweme_id
    if save_metadata:
        info_path = target_dir / f"{aweme.aweme_id}_{title}.info.json"
        info_path.write_text(
            json.dumps({
                **metadata,
                "media": [
                    {"url": media.url, "kind": media.kind,
                     "ext": media.ext, "index": media.index}
                    for media in aweme.medias
                ],
                "raw": card,
            }, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    if save_thumbnail and aweme.media_type == "video" and aweme.cover:
        from curl_cffi.requests import AsyncSession
        from app.netfp import curl_proxy_url, impersonate_for_ua
        cover_path = target_dir / f"{aweme.aweme_id}_{title}.cover.jpg"
        try:
            async with AsyncSession(
                    timeout=max(30.0, app_runtime.cfg.engine.download_timeout_seconds),
                    allow_redirects=True,
                    headers={
                        "User-Agent": actual_ua,
                        "Referer": "https://www.rednote.com/",
                    },
                    proxy=curl_proxy_url(acc_proxy) or None,
                    impersonate=impersonate_for_ua(actual_ua),
                    trust_env=False) as http:
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
        raise ShareDownloadError("海外小红书笔记解析成功，但本地没有生成媒体文件")
    return {
        "ok": True,
        "job_id": f"rednote_{aweme.aweme_id}",
        "url": source_url,
        "output_dir": str(target_dir.resolve()),
        "metadata": metadata,
        "files": files,
        "progress": {"status": "finished"},
        "warnings": [],
    }
