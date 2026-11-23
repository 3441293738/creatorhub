"""监控作品记录序列化、本地媒体定位与系统文件管理器联动。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import subprocess
import sys

from fastapi import HTTPException
from pydantic import BaseModel
from sqlmodel import select

from app import runtime as app_runtime
from app.models import ContentRecord, MonitorTarget
from app.platforms.tiktok import tiktok_display_text
from app.services import metadata as metadata_service
from app.services import thumb_cache
from app.windowing import EXPLORER_WINDOW_CLASSES, bring_window_to_front, capture_window_snapshot

_CONTENT_IMAGE_EXTS = {".jpg", ".jpeg", ".webp", ".png", ".gif", ".avif"}
_CONTENT_MEDIA_EXTS = _CONTENT_IMAGE_EXTS | {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".flv"}


def _content_media_sort_key(path: Path) -> tuple[int, int | str]:
    tail = path.stem.rsplit("_", 1)[-1]
    return (0, int(tail)) if tail.isdigit() else (1, path.name.lower())


def _content_local_media_paths(rec: ContentRecord) -> list[Path]:
    """解析监控作品对应的本地媒体文件列表（兼容单文件与图集目录，排除旁路 .cover.* 封面）。"""
    if not rec.local_path:
        return []
    try:
        path = Path(rec.local_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return []
    aid = str(rec.aweme_id or "").strip()
    if path.is_file():
        if rec.media_type == "images" and aid and path.parent.is_dir():
            try:
                siblings = [
                    child for child in path.parent.iterdir()
                    if child.is_file()
                    and child.name.startswith(f"{aid}_")
                    and ".cover." not in child.name.lower()
                    and child.suffix.lower() in _CONTENT_IMAGE_EXTS
                    and child.stat().st_size > 0
                ]
                if siblings:
                    return sorted(siblings, key=_content_media_sort_key)
            except OSError:
                pass
        try:
            return [path] if path.stat().st_size > 0 and ".cover." not in path.name.lower() else []
        except OSError:
            return []
    if not path.is_dir():
        return []
    prefix = f"{aid}_" if aid else ""
    try:
        candidates = [
            child for child in path.iterdir()
            if child.is_file()
            and (not prefix or child.name.startswith(prefix))
            and ".cover." not in child.name.lower()
            and child.suffix.lower() in _CONTENT_MEDIA_EXTS
            and child.stat().st_size > 0
        ]
    except OSError:
        return []
    return sorted(candidates, key=_content_media_sort_key)


def _content_capture_bounds(start: datetime | None, end: datetime | None):
    def utc(value):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value and value.tzinfo else value
    start, end = utc(start), utc(end)
    if start and end and start >= end:
        raise HTTPException(400, "抓取时间范围需满足开始早于结束")
    return start, end


def _content_ordering(sort: str):
    if sort == "captured_desc":
        return ContentRecord.created_at.desc(), ContentRecord.id.desc()
    if sort == "captured_asc":
        return ContentRecord.created_at.asc(), ContentRecord.id.asc()
    if sort == "create_asc":
        return ContentRecord.create_time.asc(), ContentRecord.id.asc()
    if sort == "likes_desc":
        return ContentRecord.like_count.desc(), ContentRecord.id.desc()
    if sort == "comments_desc":
        return ContentRecord.comment_count.desc(), ContentRecord.id.desc()
    return ContentRecord.create_time.desc(), ContentRecord.id.desc()


def _content_source(target_id: int, platform: str, target: MonitorTarget | None = None) -> dict:
    """Public attribution only; never serialize a task's account/token/config."""
    if target is None or target.platform != platform:
        return {"id": target_id, "name": f"已删除任务 #{target_id}" if target_id > 0 else "未关联任务",
                "platform": platform, "deleted": target_id > 0, "target_kind": "",
                "group_name": "", "tags": []}
    nickname = (tiktok_display_text(target.nickname)
                if platform == "tiktok" else target.nickname)
    if platform in {"tiktok", "instagram", "twitter"} and not nickname and target.sec_uid and not target.sec_uid.isdigit():
        nickname = f"@{target.sec_uid}"
    base = ("#" + target.keyword if target.target_kind == "keyword"
            else nickname or target.sec_uid[:12] or f"任务 #{target.id}")
    name = f"{target.alias} · {base}" if target.alias and target.alias != base else base
    return {"id": target.id, "name": name, "platform": target.platform,
            "deleted": False, "target_kind": target.target_kind,
            "group_name": target.group_name, "tags": metadata_service._load_meta_tags(target.tags)}


def _content_records(session, rows) -> list[dict]:
    ids = {row.target_id for row in rows}
    targets = {t.id: t for t in session.exec(select(MonitorTarget).where(
        MonitorTarget.id.in_(ids))).all()} if ids else {}
    return [_content_dict(row, _content_source(row.target_id, row.platform, targets.get(row.target_id)))
            for row in rows]


def _content_dict(r: ContentRecord, source: dict | None = None) -> dict:
    captured = r.created_at
    if captured:
        captured = (captured.astimezone(timezone.utc) if captured.tzinfo
                    else captured.replace(tzinfo=timezone.utc))
    has_cover = bool(thumb_cache.find_work_local_cover(r.local_path, r.aweme_id) or r.cover_url)
    cover_api_url = (
        f"/api/contents/{r.id}/cover"
        if (has_cover and r.id)
        else thumb_cache.local_thumb_api_url(r.cover_url, platform=r.platform)
    )
    return {
        "id": r.id, "platform": r.platform, "target_id": r.target_id,
        "aweme_id": r.aweme_id, "desc": r.desc, "media_type": r.media_type,
        "quality": r.quality, "create_time": r.create_time,
        "cover_url": cover_api_url,
        "remote_cover_url": r.cover_url or "",
        "like_count": r.like_count, "comment_count": r.comment_count,
        "duration": r.duration, "retry_count": r.retry_count,
        "download_status": r.download_status, "local_path": r.local_path, "error": r.error,
        "captured_at": captured.isoformat() if captured else None,
        "source": source or _content_source(r.target_id, r.platform),
    }


def _reveal_in_file_manager(path: Path):
    """Open a directory or select a file in the host OS file manager."""
    target = str(path)
    if sys.platform == "win32":
        with app_runtime._file_manager_lock:
            folder = path if path.is_dir() else path.parent
            snapshot = capture_window_snapshot(EXPLORER_WINDOW_CLASSES)
            if path.is_dir():
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["explorer.exe", "/select,", target])
            bring_window_to_front(snapshot, EXPLORER_WINDOW_CLASSES,
                                  title_hint=folder.name, timeout=2.5)
        return
    if sys.platform == "darwin":
        args = ["open", target] if path.is_dir() else ["open", "-R", target]
    else:
        args = ["xdg-open", target if path.is_dir() else str(path.parent)]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def _open_local_path(path: Path):
    """Open a local media file with the host OS default application."""
    target = str(path)
    if sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]
        return
    args = ["open", target] if sys.platform == "darwin" else ["xdg-open", target]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


class IdsIn(BaseModel):
    ids: list[int]
    with_file: bool = True
