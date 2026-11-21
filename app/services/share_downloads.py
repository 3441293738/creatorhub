"""链接解析下载历史入库、本地目录回填、Cookie 导出与本机访问校验。"""
from __future__ import annotations

from datetime import datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import json
import tempfile

from fastapi import HTTPException, Request
from sqlmodel import select

from app import runtime as app_runtime
from app.db import get_session
from app.engine.share_downloader import detect_platform
from app.models import DouyinAccount, ShareDownloadRecord
from app.settings import get_setting


def _share_input(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(400, "请粘贴分享链接或完整分享文案")
    if len(value) > 100_000:
        raise HTTPException(400, "分享内容过长（最多 100000 个字符）")
    return value


def _write_account_cookie_file(account_id: int | None) -> tuple[str, str, str]:
    """把 Patchright storage_state 临时转换为 yt-dlp 可读的 Netscape Cookie 文件。

    返回 (cookie_file, account_proxy, account_ua)。调用方必须在使用后删除 cookie_file。
    """
    if account_id is None:
        return "", "", ""
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "下载所选账号不存在")
        state_text = acc.storage_state or acc.creator_storage_state or ""
        raw_cookie = acc.cookie or ""
        platform = acc.platform
        account_proxy = acc.proxy or ""
        account_ua = acc.ua or ""

    try:
        state = json.loads(state_text or "{}")
    except Exception:
        state = {}
    cookies = list(state.get("cookies") or [])
    if not cookies and raw_cookie:
        default_domain = {
            "xhs": ".xiaohongshu.com",
            "rednote": ".rednote.com",
            "kuaishou": ".kuaishou.com",
            "shipinhao": ".weixin.qq.com",
            "tiktok": ".tiktok.com",
            "bilibili": ".bilibili.com",
            "instagram": ".instagram.com",
            "twitter": ".x.com",
        }.get(platform, ".douyin.com")
        for part in raw_cookie.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name:
                cookies.append({
                    "name": name, "value": value, "domain": default_domain,
                    "path": "/", "secure": True, "expires": 0,
                })
    if not cookies:
        raise HTTPException(400, "所选账号没有可复用的 Cookie 登录态")

    fh = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n",
        prefix="creatorhub-share-", suffix=".cookies.txt", delete=False,
    )
    try:
        fh.write("# Netscape HTTP Cookie File\n")
        for cookie in cookies:
            name = str(cookie.get("name") or "").replace("\t", "").replace("\n", "")
            value = str(cookie.get("value") or "").replace("\t", "").replace("\n", "")
            domain = str(cookie.get("domain") or "").strip()
            if not name or not domain:
                continue
            include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
            path = str(cookie.get("path") or "/").replace("\t", "")
            secure = "TRUE" if cookie.get("secure") else "FALSE"
            expires_raw = cookie.get("expires") or cookie.get("expirationDate") or 0
            try:
                expires = max(0, int(float(expires_raw)))
            except (TypeError, ValueError):
                expires = 0
            fh.write(
                f"{domain}\t{include_subdomains}\t{path}\t{secure}\t"
                f"{expires}\t{name}\t{value}\n"
            )
    finally:
        fh.close()
    return fh.name, account_proxy, account_ua


def _share_file_role(path: Path) -> str:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name.endswith(".info.json"):
        return "metadata"
    if ".cover." in name:
        return "thumbnail"
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".avif"}:
        return "media"
    if suffix in {".srt", ".vtt", ".ass", ".lrc", ".ttml"}:
        return "subtitle"
    if suffix in {
        ".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi", ".m4v",
        ".mp3", ".m4a", ".aac", ".opus", ".ogg", ".wav", ".flac",
    }:
        return "media"
    if suffix in {".json", ".description"}:
        return "metadata"
    return "other"


_SHARE_HISTORY_META_KEYS = {
    "id", "title", "description", "uploader", "uploader_id", "channel",
    "duration", "timestamp", "upload_date", "view_count", "like_count",
    "comment_count", "thumbnail", "webpage_url", "original_url",
    "extractor", "extractor_key", "ext", "format", "format_id",
    "width", "height", "platform", "media_type", "media_count",
}


def _compact_share_metadata(metadata: Any) -> dict:
    """只保留历史列表需要的字段，避免把 yt-dlp 的完整响应重复写进数据库。"""
    if not isinstance(metadata, dict):
        return {}
    return {
        key: value for key, value in metadata.items()
        if key in _SHARE_HISTORY_META_KEYS
    }


def _save_share_download_history(
    *,
    source_url: str,
    platform: str,
    account_id: int | None,
    item: dict,
) -> int:
    metadata = _compact_share_metadata(item.get("metadata"))
    files = item.get("files") if isinstance(item.get("files"), list) else []
    media_files = [file for file in files if file.get("role") == "media"]
    status = "done" if item.get("ok") else "failed"
    record = ShareDownloadRecord(
        platform=platform or str(metadata.get("platform") or detect_platform(source_url)),
        source_url=source_url,
        account_id=account_id,
        item_id=str(metadata.get("id") or ""),
        title=str(metadata.get("title") or ""),
        author=str(metadata.get("uploader") or metadata.get("channel") or ""),
        media_type=str(metadata.get("media_type") or ""),
        media_count=int(metadata.get("media_count") or len(media_files)),
        cover_url=str(metadata.get("thumbnail") or ""),
        status=status,
        output_dir=str(item.get("output_dir") or ""),
        files_json=json.dumps(files, ensure_ascii=False, default=str),
        metadata_json=json.dumps(metadata, ensure_ascii=False, default=str),
        error=str(item.get("error") or ""),
    )
    with get_session() as s:
        s.add(record)
        s.commit()
        s.refresh(record)
        return int(record.id or 0)


def _backfill_share_download_history() -> int:
    """从已有的 ``*.info.json`` 补录旧下载，升级后历史列表不会是空的。"""
    default_root = get_setting("download_dir", app_runtime.cfg.engine.media_dir) or app_runtime.cfg.engine.media_dir
    share_root = Path(default_root).expanduser() / "share"
    if not share_root.is_dir():
        return 0

    restored = 0
    # 防止用户把超大归档目录设成下载目录时启动扫描失控。
    for info_path in list(share_root.rglob("*.info.json"))[:5000]:
        try:
            metadata_raw = json.loads(info_path.read_text(encoding="utf-8"))
            if not isinstance(metadata_raw, dict):
                continue
            metadata = _compact_share_metadata(metadata_raw)
            item_id = str(metadata.get("id") or "")
            source_url = str(
                metadata.get("webpage_url")
                or metadata.get("original_url")
                or ""
            )
            if not item_id and not source_url:
                continue

            with get_session() as s:
                query = select(ShareDownloadRecord.id)
                if item_id:
                    query = query.where(ShareDownloadRecord.item_id == item_id)
                else:
                    query = query.where(ShareDownloadRecord.source_url == source_url)
                if s.exec(query.limit(1)).first() is not None:
                    continue

            prefix = f"{item_id}_" if item_id else info_path.name[:-10]
            files = []
            for path in sorted(info_path.parent.iterdir()):
                if not path.is_file() or path.suffix.lower() in {".part", ".ytdl"}:
                    continue
                # 原生抖音目录可能含多个作品，只关联相同作品 ID 前缀的文件。
                if item_id and not path.name.startswith(prefix):
                    continue
                try:
                    relative = path.relative_to(share_root).as_posix()
                    size = path.stat().st_size
                except OSError:
                    continue
                files.append({
                    "name": path.name,
                    "path": str(path.resolve()),
                    "relative_path": relative,
                    "size": size,
                    "role": _share_file_role(path),
                })

            platform = str(metadata.get("platform") or detect_platform(source_url))
            _save_share_download_history(
                source_url=source_url,
                platform=platform,
                account_id=None,
                item={
                    "ok": True,
                    "output_dir": str(info_path.parent.resolve()),
                    "metadata": metadata,
                    "files": files,
                },
            )
            restored += 1
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return restored


def _share_history_dict(record: ShareDownloadRecord) -> dict:
    try:
        files = json.loads(record.files_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        files = []
    if not isinstance(files, list):
        files = []
    try:
        metadata = json.loads(record.metadata_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    media_files = [item for item in files if isinstance(item, dict) and item.get("role") == "media"]
    first_file = media_files[0] if media_files else (files[0] if files and isinstance(files[0], dict) else {})

    def number(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    description = str(record.title or metadata.get("title") or metadata.get("description") or "")
    create_time = number(metadata.get("timestamp"))
    if not create_time:
        upload_date = str(metadata.get("upload_date") or "")
        if len(upload_date) == 8 and upload_date.isdigit():
            try:
                create_time = int(datetime.strptime(upload_date, "%Y%m%d").timestamp())
            except ValueError:
                create_time = 0
    local_path = str(first_file.get("path") or "") if isinstance(first_file, dict) else ""
    quality = str(metadata.get("format") or metadata.get("format_id") or "")
    return {
        "id": record.id,
        "platform": record.platform,
        "source_url": record.source_url,
        "account_id": record.account_id,
        "item_id": record.item_id,
        "title": record.title,
        "author": record.author,
        "media_type": record.media_type,
        "media_count": record.media_count,
        "cover_url": record.cover_url,
        "status": record.status,
        "output_dir": record.output_dir,
        "files": files if isinstance(files, list) else [],
        "metadata": metadata if isinstance(metadata, dict) else {},
        "error": record.error,
        "created_at": record.created_at.isoformat() if record.created_at else "",
        # 与「作品监控」列表兼容的展示字段，方便链接下载历史复用同一套作品表格。
        "aweme_id": record.item_id,
        "desc": description,
        "create_time": create_time,
        "quality": quality,
        "like_count": number(metadata.get("like_count")),
        "comment_count": number(metadata.get("comment_count")),
        "duration": number(metadata.get("duration")),
        "download_status": record.status,
        "local_path": local_path,
    }


def _require_local_action(request: Request, action: str = "reveal") -> None:
    """仅允许本机 CreatorHub 页面触发文件管理器操作。"""
    def is_loopback(host: str) -> bool:
        host = host.split("%", 1)[0].casefold()
        if host == "localhost":
            return True
        try:
            return ip_address(host).is_loopback
        except ValueError:
            return False

    client_host = request.client.host if request.client else ""
    page_host = request.url.hostname or ""
    try:
        origin = urlsplit(request.headers.get("origin", ""))
        same_origin = (origin.scheme == request.url.scheme and
                       (origin.hostname or "").casefold() == page_host.casefold() and
                       origin.port == request.url.port)
    except ValueError:
        same_origin = False
    local_action = request.headers.get("x-creatorhub-local-action") == action
    if not is_loopback(client_host) or not is_loopback(page_host) or \
            not same_origin or not local_action:
        raise HTTPException(403, "仅允许从本机 CreatorHub 页面执行文件操作")
