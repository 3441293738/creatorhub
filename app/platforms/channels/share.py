"""微信视频号分享链接解析与加密媒体下载服务(供 `/api/share-download` 与工具层调用)。

解析与下载优先级:
  1. 若已配置且可连接微信原生 Finder 本地 CDP 通道(`channels_cdp_endpoint` / `channels_cdp_endpoint_file`)，
     优先调用 `finderGetCommentDetail` 获取完整 `ChannelsObject`(含高码率 CDN `url + urlToken`、
     精确十进制 `decodeKey`、`objectNonceId` 及互动计数)，流式下载后执行 `WxIsaac64` 128KiB 解密与
     MP4 Box 结构核验；
  2. 否则调用公开预览接口 `finder-preview/api/feed/get_feed_info`，
     诚实返回可访问的标题、作者、时间、封面及可能存在的公开媒体流(绝不拿封面冒充视频)。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from ...engine.downloader import Downloader
from ...engine.share_downloader import ShareDownloadError, ShareDownloadFallback
from .client import ChannelsProtocolClient
from .extract import Aweme, parse_channels_feed, safe_title
from .resolve import (
    ChannelsPreviewError,
    fetch_finder_preview_info,
    is_channels_share_url,
    parse_channels_url,
)


def _file_role(path: Path) -> str:
    suffix = path.suffix.lower()
    if path.name.lower().endswith(".info.json"):
        return "metadata"
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".avif"}:
        return "thumbnail"
    if suffix in {".json", ".description"}:
        return "metadata"
    if suffix in {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".aac", ".opus", ".wav", ".flac"}:
        return "media"
    return "other"


def _aweme_share_metadata(aweme: Aweme, source_url: str, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    ts = int(aweme.create_time or 0)
    upload_date = datetime.fromtimestamp(ts).strftime("%Y%m%d") if ts > 0 else None
    title_line = (aweme.desc or "").splitlines()[0].strip() if aweme.desc else aweme.aweme_id
    meta: dict[str, Any] = {
        "id": aweme.aweme_id,
        "title": title_line[:120] or aweme.aweme_id,
        "description": aweme.desc,
        "uploader": aweme.author_name,
        "uploader_id": aweme.author_unique_id or None,
        "duration": aweme.duration or None,
        "timestamp": ts or None,
        "upload_date": upload_date,
        "like_count": aweme.like_count,
        "comment_count": aweme.comment_count,
        "thumbnail": aweme.cover or None,
        "webpage_url": aweme.source_url or source_url,
        "original_url": source_url,
        "extractor": "wechat_channels_native",
        "extractor_key": "WeChatChannels",
        "ext": "mp4" if aweme.media_type == "video" else "jpeg",
        "format": aweme.quality_label or ("images" if aweme.media_type == "images" else "mp4"),
        "platform": "wechat",
        "video_url_available": bool(aweme.medias),
    }
    if aweme.object_nonce_id:
        meta["object_nonce_id"] = aweme.object_nonce_id
    if extra:
        for k, v in extra.items():
            if v is not None and k not in meta:
                meta[k] = v
    return {k: v for k, v in meta.items() if v is not None}


def _extract_audio_from_mp4(video_path: Path, ffmpeg_bin: str) -> Path:
    audio_path = video_path.with_suffix(".m4a")
    res = subprocess.run(
        [
            ffmpeg_bin, "-y", "-hide_banner", "-v", "error",
            "-i", str(video_path), "-vn", "-c:a", "aac", "-b:a", "192k", str(audio_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if res.returncode != 0 or not audio_path.exists() or audio_path.stat().st_size == 0:
        audio_path.unlink(missing_ok=True)
        raise ShareDownloadError(f"视频号音频提取失败: {res.stderr.strip() or res.returncode}")
    video_path.unlink(missing_ok=True)
    return audio_path


async def inspect_or_download_channels_share(
    source_url: str,
    *,
    cfg: Any,
    output_root: Path,
    quality: str = "highest",
    should_download: bool = True,
    save_metadata: bool = True,
    save_thumbnail: bool = True,
    proxy: str = "",
    user_agent: str = "",
    account_storage_state: str = "",
) -> dict[str, Any]:
    """解析或下载一条微信视频号分享链接。非视频号的 qq.com 链接抛出 ShareDownloadFallback 交通用提取器。"""
    if not is_channels_share_url(source_url):
        raise ShareDownloadFallback("非微信视频号域名路径，交由通用提取器处理")

    ref = parse_channels_url(source_url)
    if ref is None:
        raise ShareDownloadError("无法识别该视频号链接格式，请粘贴 weixin.qq.com/sph/... 或视频号作品链接")
    if ref.kind == "creator":
        raise ShareDownloadError(
            f"该链接对应视频号创作者主页({ref.username})，不是单条作品；请在「作品采集 → 创作者主页」或「作品监控」中使用"
        )

    read_mode = str(getattr(cfg.engine, "channels_read_mode", "auto") or "auto").strip().lower()
    ua = user_agent or str(getattr(cfg.engine, "user_agent", "") or "")
    timeout = max(20.0, float(getattr(cfg.engine, "request_timeout_seconds", 20.0) or 20.0))

    warnings: list[str] = []
    raw_source: dict[str, Any] = {}
    aweme: Aweme | None = None

    # 1. 原生微信 Finder CDP 协议优先(可拿 decodeKey + urlToken + objectNonceId)
    proto_client = ChannelsProtocolClient.from_config(cfg) if read_mode != "browser" else None
    if proto_client is not None:
        try:
            async with proto_client:
                obj, proto_err = await proto_client.fetch_feed_detail(
                    object_id=ref.object_id,
                    object_nonce_id=ref.object_nonce_id,
                    export_id=ref.export_id,
                    share_url=source_url,
                )
            if obj:
                raw_source = obj
                aweme = parse_channels_feed(obj, quality=quality if quality != "audio" else "highest")
            elif proto_err:
                if read_mode == "protocol":
                    raise ShareDownloadError(proto_err)
                warnings.append(f"{proto_err}；已回退公开预览接口")
        except ShareDownloadError:
            raise
        except Exception as exc:
            if read_mode == "protocol":
                raise ShareDownloadError(f"视频号原生协议不可用: {exc}") from exc
            warnings.append(f"本地微信 Finder 原生协议未连接({exc})，已使用公开预览接口")

    # 2. 公开预览 get_feed_info 回退
    if aweme is None or (should_download and not aweme.medias):
        preview_info: dict[str, Any] | None = None
        preview_error = ""

        if ref.share_id or ref.export_id:
            try:
                preview_info = await fetch_finder_preview_info(
                    share_id=ref.share_id,
                    export_id=ref.export_id,
                    general_token=ref.general_token,
                    user_agent=ua,
                    proxy=proxy,
                    timeout=timeout,
                )
            except ChannelsPreviewError as exc:
                preview_error = str(exc)
            except Exception as exc:
                preview_error = f"公开预览请求异常: {exc}"

        if preview_info is not None:
            raw_source = preview_info
            preview_aweme = parse_channels_feed(
                preview_info, quality=quality if quality != "audio" else "highest"
            )
            if aweme is None or (preview_aweme and preview_aweme.medias and not aweme.medias):
                aweme = preview_aweme
        elif aweme is None:
            raise ShareDownloadError(
                preview_error
                or "视频号链接未暴露公开预览数据，请配置桌面微信 Finder 本地 CDP 通道(channels_cdp_endpoint)后重试"
            )

    assert aweme is not None
    metadata = _aweme_share_metadata(
        aweme,
        source_url,
        extra={
            "share_id": ref.share_id or None,
            "export_id": raw_source.get("export_id") or ref.export_id or None,
            "decrypted_with": "WxIsaac64-128KiB" if any(m.decode_key for m in aweme.medias) else None,
        },
    )

    if not aweme.medias:
        warnings.append(
            "公开分享预览仅返回标题/作者等元数据，未暴露未加密或带 decodeKey 的视频流；"
            "如需下载完整加密视频并离线 WxIsaac64 解码，请连接桌面微信 Finder 原生 CDP 通道"
        )

    if not should_download:
        return {
            "ok": True,
            "url": source_url,
            "metadata": metadata,
            "warnings": warnings,
        }

    if not aweme.medias:
        raise ShareDownloadError(
            f"已解析视频号作品「{aweme.desc[:40] or aweme.aweme_id}」(作者：{aweme.author_name or '未知'})，"
            "但公开分享预览未返回媒体直链与 decodeKey；请连接桌面微信 Finder 原生 CDP 通道后下载完整视频"
        )

    audio_only = (quality or "").strip().lower() in {"audio", "音频"}
    ffmpeg_bin = ""
    if audio_only and aweme.media_type == "video":
        from ...runtime_tools import find_ffmpeg

        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            raise ShareDownloadError("提取视频号纯音频需要安装 ffmpeg")

    dl_timeout = max(30.0, float(getattr(cfg.engine, "download_timeout_seconds", 120.0) or 120.0))
    downloader = Downloader(str(output_root), ua, timeout=dl_timeout)
    ok, _local_path, dl_err = await downloader.download_aweme(
        aweme,
        base_dir=str(output_root),
        proxy=proxy,
        user_agent=ua,
        referer="https://channels.weixin.qq.com/",
    )
    if not ok:
        raise ShareDownloadError(dl_err or "视频号媒体下载或解密失败")

    target_dir = output_root / safe_title(aweme.author_name or "unknown")
    title = safe_title(aweme.desc) if str(aweme.desc or "").strip() else aweme.aweme_id

    if audio_only and aweme.media_type == "video":
        video_file = target_dir / f"{aweme.aweme_id}_{title}.mp4"
        if video_file.is_file():
            await asyncio.to_thread(_extract_audio_from_mp4, video_file, ffmpeg_bin)

    if save_metadata:
        info_path = target_dir / f"{aweme.aweme_id}_{title}.info.json"
        info_path.write_text(
            json.dumps(
                {
                    **metadata,
                    "media": [
                        {
                            "url": m.url,
                            "kind": m.kind,
                            "ext": m.ext,
                            "index": m.index,
                            "encrypted": bool(m.decode_key),
                        }
                        for m in aweme.medias
                    ],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    if save_thumbnail and aweme.media_type == "video" and aweme.cover:
        from ...browser.manager import normalize_proxy

        cover_path = target_dir / f"{aweme.aweme_id}_{title}.cover.jpg"
        try:
            async with httpx.AsyncClient(
                timeout=dl_timeout,
                follow_redirects=True,
                headers={"User-Agent": ua, "Referer": "https://channels.weixin.qq.com/"},
                proxy=normalize_proxy(proxy) if proxy else None,
            ) as http_cli:
                await downloader._download_one(http_cli, aweme.cover, cover_path)
        except Exception:
            pass

    files: list[dict[str, Any]] = []
    for path in sorted(target_dir.glob(f"{aweme.aweme_id}_*")):
        if not path.is_file() or path.suffix.lower() in {".part", ".enc", ".ytdl"}:
            continue
        files.append({
            "name": path.name,
            "path": str(path.resolve()),
            "relative_path": path.relative_to(output_root).as_posix(),
            "size": path.stat().st_size,
            "role": _file_role(path),
        })
    if not any(f["role"] == "media" for f in files):
        raise ShareDownloadError("视频号作品解析完成，但本地未生成有效媒体文件")

    return {
        "ok": True,
        "job_id": f"channels_{aweme.aweme_id}",
        "url": source_url,
        "output_dir": str(target_dir.resolve()),
        "metadata": metadata,
        "files": files,
        "progress": {"status": "finished"},
        "warnings": warnings,
    }
