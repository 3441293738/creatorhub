"""评论监控与弹幕监控来源解析、存量字段回填及分页排序公共逻辑。"""
from __future__ import annotations

from datetime import timezone
import json

from fastapi import HTTPException
from sqlalchemy import or_
from sqlmodel import select

from app.db import get_session
from app.models import CommentRecord, CommentWatch, DanmakuRecord, DanmakuWatch
from app.platforms.douyin import parse_danmaku
from app.services import metadata as metadata_service


# ─────────── 评论 / 弹幕记录来源 ───────────
def _watch_source(watch_id: int | None, platform: str, model, watch=None) -> dict:
    label = "评论" if model is CommentWatch else "弹幕"
    module = "comments" if model is CommentWatch else "danmaku"
    wid = watch_id or 0
    missing = watch is None or watch.platform != platform
    base = "" if missing else (watch.title or watch.aweme_id or watch.sec_uid[:12] or f"任务 #{wid}")
    name = (f"已删除{label}任务 #{wid}" if wid > 0 else f"未关联{label}监控") if missing else (
        f"{watch.alias} · {base}" if watch.alias and watch.alias != base else base)
    return {"id": wid, "module": module, "platform": platform, "name": name,
            "deleted": missing and wid > 0, "unassigned": wid <= 0,
            "kind": "" if missing else watch.kind,
            "group_name": "" if missing else watch.group_name,
            "tags": [] if missing else metadata_service._load_meta_tags(watch.tags)}


def _selected_watch_source(session, model, watch_id, platform):
    if watch_id is None:
        return None
    watch = session.get(model, watch_id) if watch_id > 0 else None
    return _watch_source(watch_id, platform or (watch.platform if watch else ""), model, watch)


def _watch_record_filter(stmt, model, watch_id):
    if watch_id is None:
        return stmt
    if watch_id < 0:
        raise HTTPException(400, "来源监控编号应为非负整数")
    if watch_id == 0:
        return stmt.where(or_(model.watch_id == 0, model.watch_id.is_(None)))
    return stmt.where(model.watch_id == watch_id)


def _watch_record_ordering(model, sort):
    if sort == "captured_asc":
        return model.created_at.asc(), model.id.asc()
    if sort == "captured_desc":
        return model.created_at.desc(), model.id.desc()
    if model is CommentRecord:
        if sort == "oldest":
            return model.create_time.asc(), model.id.asc()
        if sort == "likes_desc":
            return model.like_count.desc(), model.id.desc()
        return model.create_time.desc(), model.id.desc()
    if sort == "video_desc":
        return model.video_time_ms.desc(), model.id.desc()
    return model.video_time_ms.asc(), model.id.asc()


def _watch_captured_at(value):
    if value is None:
        return None
    return (value.astimezone(timezone.utc) if value.tzinfo
            else value.replace(tzinfo=timezone.utc)).isoformat()


def _watch_records(session, rows, model):
    ids = {row.watch_id for row in rows if row.watch_id and row.watch_id > 0}
    watches = {w.id: w for w in session.exec(select(model).where(model.id.in_(ids))).all()} if ids else {}
    serialize = _comment_dict if model is CommentWatch else _danmaku_dict
    return [serialize(row, _watch_source(row.watch_id, row.platform, model, watches.get(row.watch_id)))
            for row in rows]


# ─────────── 评论数据 ───────────
def _comment_dict(c: CommentRecord, watch_source: dict | None = None) -> dict:
    return {
        "id": c.id, "watch_id": c.watch_id, "aweme_id": c.aweme_id,
        "comment_id": c.comment_id, "text": c.text, "user_nickname": c.user_nickname,
        "user_sec_uid": c.user_sec_uid, "ip_label": c.ip_label,
        "like_count": c.like_count, "create_time": c.create_time,
        "is_reply": bool(c.reply_to),
        "platform": c.platform, "captured_at": _watch_captured_at(c.created_at),
        "watch_source": watch_source or _watch_source(c.watch_id, c.platform, CommentWatch),
    }


def _parse_stored_danmaku(row: DanmakuRecord) -> dict | None:
    if not row.raw_json:
        return None
    try:
        raw = json.loads(row.raw_json)
    except Exception:
        return None
    return parse_danmaku(raw, row.aweme_id or "")


def _backfill_danmaku_records() -> int:
    """用 raw_json 修复接口改版前已入库的 offset_time/user_id 等字段。"""
    repaired = 0
    with get_session() as s:
        rows = s.exec(select(DanmakuRecord)).all()
        for row in rows:
            parsed = _parse_stored_danmaku(row)
            if not parsed:
                continue
            changed = False
            for name in ("aweme_id", "user_id", "user_nickname", "video_time_ms",
                         "create_time", "like_count", "is_blocked"):
                current = getattr(row, name)
                value = parsed.get(name)
                if (not current) and value not in (None, "", 0, False):
                    setattr(row, name, value)
                    changed = True
            if changed:
                s.add(row)
                repaired += 1
        if repaired:
            s.commit()
    return repaired


def _danmaku_dict(row: DanmakuRecord, watch_source: dict | None = None) -> dict:
    parsed = _parse_stored_danmaku(row)
    user_id = row.user_id or (parsed or {}).get("user_id", "")
    user_nickname = row.user_nickname or (parsed or {}).get("user_nickname", "")
    point = max(0, int(row.video_time_ms or (parsed or {}).get("video_time_ms", 0) or 0))
    return {
        "id": row.id, "watch_id": row.watch_id, "aweme_id": row.aweme_id,
        "danmaku_id": row.danmaku_id, "text": row.text,
        "user_id": user_id, "user_nickname": user_nickname,
        "video_time_ms": point, "video_time": point / 1000,
        "create_time": row.create_time, "like_count": row.like_count,
        "is_blocked": row.is_blocked, "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "platform": row.platform, "captured_at": _watch_captured_at(row.created_at),
        "watch_source": watch_source or _watch_source(row.watch_id, row.platform, DanmakuWatch),
    }
