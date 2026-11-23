"""多平台全局指挥大盘、账号矩阵分组与批量运维、跨账号数据对比、统一内容画廊快览与矩阵群发接口。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import select

from app import runtime as app_runtime
from app.api.accounts import _clean_account_tags, _parse_account_tags
from app.api.publishing import _publish_dict, _publish_title_cap, _validate_non_xhs_publish_files
from app.browser.backends import ACCOUNT_BROWSER_BACKENDS, LOCAL_BACKEND
from app.browser.manager import normalize_proxy
from app.profiles import assign_proxy_from_pool, inspect_account_proxy_risk, resolve_pool_proxy_row
from app.db import get_session
from app.models import (
    AccountActionTask,
    AccountRiskState,
    AccountStatSnapshot,
    AccountWork,
    CommentRecord,
    CommentTask,
    ContentRecord,
    DmConversation,
    DouyinAccount,
    KeywordCollectionComment,
    KeywordCollectionContent,
    KeywordCollectionJob,
    MatrixGroup,
    MonitorTarget,
    PublishTask,
    ShareDownloadRecord,
)
from app.platforms.rednote import has_web_session as rednote_has_web_session, weighted_truncate
from app.platforms.xhs import has_creator_cookies
from app.scheduling import utc_iso
from app.services import account_profiles as account_profiles_service
from app.services import browser_runtimes as browser_runtimes_service
from app.services import contents as contents_service
from app.services import login_sessions as login_sessions_service
from app.services import metadata as metadata_service
from app.services import proxies as proxies_service
from app.services import share_downloads as share_downloads_service
from app.services import thumb_cache

router = APIRouter()

ALL_PLATFORMS: tuple[str, ...] = (
    "douyin",
    "xhs",
    "rednote",
    "kuaishou",
    "shipinhao",
    "tiktok",
    "bilibili",
    "instagram",
    "twitter",
)

GROUP_COLORS = {"blue", "rose", "emerald", "amber", "violet", "cyan", "slate"}
MEDIA_EXTENSIONS = {
    ".mp4", ".mov", ".webm", ".mkv", ".m4v",
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif",
}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


def _norm_platform(platform: str | None) -> str:
    val = str(platform or "").strip().lower()
    if not val or val == "all":
        return ""
    if val not in ALL_PLATFORMS:
        raise HTTPException(422, "平台类型无效")
    return val


def _account_display_name(acc: DouyinAccount | None, fallback_id: int | None = None) -> str:
    if acc is None:
        return f"账号 #{fallback_id}" if fallback_id else "未指定账号"
    return (acc.alias or "").strip() or (acc.nickname or "").strip() or f"账号 #{acc.id}"


def _account_login_flags(acc: DouyinAccount) -> tuple[bool, bool]:
    has_creator = (
        bool(acc.creator_storage_state)
        or (
            acc.platform in ("kuaishou", "douyin", "shipinhao", "tiktok", "bilibili", "rednote", "instagram", "twitter")
            and bool(acc.storage_state)
        )
        or (acc.platform == "xhs" and has_creator_cookies(acc.storage_state))
    )
    has_read_login = (
        account_profiles_service._xhs_has_read_login_state(acc.storage_state)
        if acc.platform == "xhs"
        else rednote_has_web_session(acc.storage_state)
        if acc.platform == "rednote"
        else bool(acc.storage_state)
    )
    return has_read_login, has_creator


def _compute_fans_deltas(session, account_ids: list[int]) -> dict[int, int]:
    if not account_ids:
        return {}
    rows = session.exec(
        select(AccountStatSnapshot)
        .where(AccountStatSnapshot.account_id.in_(account_ids))
        .order_by(AccountStatSnapshot.account_id, AccountStatSnapshot.date.desc())
    ).all()
    grouped: dict[int, list[AccountStatSnapshot]] = {}
    for row in rows:
        bucket = grouped.setdefault(row.account_id, [])
        if len(bucket) < 2:
            bucket.append(row)
    deltas: dict[int, int] = {}
    for aid, snaps in grouped.items():
        if len(snaps) >= 2:
            deltas[aid] = int(snaps[0].follower_count or 0) - int(snaps[1].follower_count or 0)
        else:
            deltas[aid] = 0
    return deltas


def _compute_task_rollups(session, account_ids: list[int], now: datetime) -> dict[int, dict[str, int]]:
    rollups = {
        aid: {"total": 0, "pending": 0, "running": 0, "blocked": 0, "failed": 0}
        for aid in account_ids
    }
    if not account_ids:
        return rollups
    task_specs = (
        (KeywordCollectionJob, {"pending"}, {"running"}),
        (PublishTask, {"draft", "pending"}, {"publishing"}),
        (CommentTask, {"draft", "pending"}, {"doing"}),
        (AccountActionTask, {"draft", "pending"}, {"doing"}),
    )
    for model, pending_states, running_states in task_specs:
        statuses = list(pending_states | running_states | metadata_service._QUEUE_FAILED)
        rows = session.exec(
            select(model).where(
                model.account_id.in_(account_ids),
                model.status.in_(statuses),
            )
        ).all()
        for row in rows:
            item = rollups.get(row.account_id)
            if item is None:
                continue
            item["total"] += 1
            if row.status in metadata_service._QUEUE_FAILED:
                item["failed"] += 1
            elif getattr(row, "blocked_reason", "") or (
                getattr(row, "next_allowed_at", None) and row.next_allowed_at > now
            ):
                item["blocked"] += 1
            elif row.status in running_states:
                item["running"] += 1
            else:
                item["pending"] += 1
    return rollups


def _account_health_state(
    acc: DouyinAccount,
    *,
    has_read_login: bool,
    has_creator: bool,
    risk_state: AccountRiskState | None,
    now: datetime,
    same_platform_direct_count: int = 0,
    same_platform_total_count: int = 0,
    same_platform_shared_proxy: bool = False,
) -> str:
    if acc.status == "invalid" or (not has_read_login and not has_creator):
        return "invalid"
    if acc.proxy and acc.proxy_status in {"bad", "auth_error", "blocked", "disabled"}:
        return "proxy_issue"
    if acc.proxy and acc.fp_source_ip and acc.exit_ip and acc.fp_source_ip != acc.exit_ip:
        return "proxy_issue"
    if risk_state and (
        risk_state.manual_review_required
        or (risk_state.cooldown_until and risk_state.cooldown_until > now)
        or int(risk_state.risk_level or 0) > 0
    ):
        return "risk"
    if acc.write_paused_until and acc.write_paused_until > now:
        return "risk"
    if same_platform_shared_proxy:
        return "shared_proxy"
    if not acc.proxy and (
        acc.platform in {"tiktok", "rednote", "instagram", "twitter"}
        or same_platform_total_count > 1
        or same_platform_direct_count > 1
    ):
        return "no_proxy"
    return "healthy"


# ─────────────────── 全局与单平台自适应指挥大盘 ───────────────────

@router.get("/api/matrix/overview")
async def matrix_overview(platform: str = ""):
    pf_filter = _norm_platform(platform)
    now = datetime.utcnow()
    local_now = datetime.now().astimezone()
    today_start_utc = (
        local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )

    with get_session() as session:
        all_accounts = session.exec(select(DouyinAccount)).all()
        all_ids = [a.id for a in all_accounts if a.id is not None]
        risk_map = {
            r.account_id: r
            for r in (
                session.exec(select(AccountRiskState).where(AccountRiskState.account_id.in_(all_ids))).all()
                if all_ids
                else []
            )
        }
        fans_deltas = _compute_fans_deltas(session, all_ids)
        rollups = _compute_task_rollups(session, all_ids, now)

        # 同平台未配代理计数与同平台共享代理/出口 IP 计数（仅用于风险提醒，不阻断运行）
        direct_by_pf: dict[str, int] = {}
        total_by_pf: dict[str, int] = {}
        same_pf_proxy_counts: dict[tuple[str, str], int] = {}
        same_pf_exit_counts: dict[tuple[str, str], int] = {}
        for acc in all_accounts:
            total_by_pf[acc.platform] = total_by_pf.get(acc.platform, 0) + 1
            if not acc.proxy:
                direct_by_pf[acc.platform] = direct_by_pf.get(acc.platform, 0) + 1
            else:
                np = normalize_proxy(acc.proxy)
                if np:
                    same_pf_proxy_counts[(acc.platform, np)] = same_pf_proxy_counts.get((acc.platform, np), 0) + 1
            ex_ip = str(acc.exit_ip or "").strip()
            if ex_ip:
                same_pf_exit_counts[(acc.platform, ex_ip)] = same_pf_exit_counts.get((acc.platform, ex_ip), 0) + 1

        # 未读私信按账号与平台汇总
        unread_by_acc: dict[int, int] = {}
        unread_by_pf: dict[str, int] = {}
        for row in session.exec(select(DmConversation).where(DmConversation.unread_count > 0)).all():
            cnt = int(row.unread_count or 0)
            unread_by_acc[row.account_id] = unread_by_acc.get(row.account_id, 0) + cnt
            unread_by_pf[row.platform] = unread_by_pf.get(row.platform, 0) + cnt

        # 监控数按平台汇总
        monitors = session.exec(select(MonitorTarget)).all()
        mon_enabled_by_pf: dict[str, int] = {}
        mon_name_map: dict[int, str] = {}
        for m in monitors:
            if m.id is not None:
                base = f"#{m.keyword}" if m.target_kind == "keyword" else (m.nickname or (m.sec_uid or "")[:12])
                mon_name_map[m.id] = f"{m.alias} · {base}" if m.alias else base
            if m.enabled:
                mon_enabled_by_pf[m.platform] = mon_enabled_by_pf.get(m.platform, 0) + 1

        # 近 7 日趋势 + 今日各平台新增计数
        days_count = 7
        today_date = now.date()
        day_labels = [(today_date - timedelta(days=days_count - 1 - i)).isoformat() for i in range(days_count)]
        day_index = {d: i for i, d in enumerate(day_labels)}
        contents_7d = [0] * days_count
        comments_7d = [0] * days_count
        publishes_7d = [0] * days_count
        today_contents_by_pf: dict[str, int] = {}
        today_comments_by_pf: dict[str, int] = {}

        all_content_meta = session.exec(
            select(ContentRecord.platform, ContentRecord.created_at, ContentRecord.download_status)
        ).all()
        contents_total_scope = 0
        downloaded_total_scope = 0
        for pf, ts, dl_st in all_content_meta:
            if ts and ts >= today_start_utc:
                today_contents_by_pf[pf] = today_contents_by_pf.get(pf, 0) + 1
            if not pf_filter or pf == pf_filter:
                contents_total_scope += 1
                if dl_st == "done":
                    downloaded_total_scope += 1
                if ts:
                    key = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
                    idx = day_index.get(key)
                    if idx is not None:
                        contents_7d[idx] += 1

        # 同时把关键词批量采集今日新入库计入平台态势与趋势
        for pf, ts in session.exec(
            select(KeywordCollectionContent.platform, KeywordCollectionContent.created_at)
        ).all():
            if ts and ts >= today_start_utc:
                today_contents_by_pf[pf] = today_contents_by_pf.get(pf, 0) + 1
            if (not pf_filter or pf == pf_filter) and ts:
                key = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
                idx = day_index.get(key)
                if idx is not None:
                    contents_7d[idx] += 1

        comments_total_scope = 0
        for pf, ts in session.exec(select(CommentRecord.platform, CommentRecord.created_at)).all():
            if ts and ts >= today_start_utc:
                today_comments_by_pf[pf] = today_comments_by_pf.get(pf, 0) + 1
            if not pf_filter or pf == pf_filter:
                comments_total_scope += 1
                if ts:
                    key = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
                    idx = day_index.get(key)
                    if idx is not None:
                        comments_7d[idx] += 1

        for pf, ts, st in session.exec(
            select(PublishTask.platform, PublishTask.created_at, PublishTask.status)
        ).all():
            if (not pf_filter or pf == pf_filter) and ts and st in {"done", "publishing", "pending"}:
                key = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
                idx = day_index.get(key)
                if idx is not None:
                    publishes_7d[idx] += 1

        # 自有作品数与异常作品数
        own_works_scope = 0
        abnormal_works_scope = 0
        for pf, zero_alert, status_alert in session.exec(
            select(AccountWork.platform, AccountWork.zero_play_alerted, AccountWork.status_alerted)
        ).all():
            if not pf_filter or pf == pf_filter:
                own_works_scope += 1
                if zero_alert or bool(status_alert):
                    abnormal_works_scope += 1

        # 待人工审核的评论/私信草稿任务
        pending_reviews = 0
        for pf in session.exec(select(CommentTask.platform).where(CommentTask.status == "draft")).all():
            if not pf_filter or pf == pf_filter:
                pending_reviews += 1
        for pf in session.exec(select(AccountActionTask.platform).where(AccountActionTask.status == "draft")).all():
            if not pf_filter or pf == pf_filter:
                pending_reviews += 1

        # 构建 9 平台态势条 (供顶部导航栏徽章 & 全局总览平台卡片墙共用)
        strip_map: dict[str, dict[str, Any]] = {
            pf: {
                "platform": pf,
                "accounts_total": 0,
                "accounts_active": 0,
                "accounts_invalid": 0,
                "accounts_risk": 0,
                "accounts_proxy_issue": 0,
                "accounts_shared_proxy": 0,
                "accounts_no_proxy": 0,
                "followers_total": 0,
                "fans_delta": 0,
                "monitors_enabled": mon_enabled_by_pf.get(pf, 0),
                "today_contents": today_contents_by_pf.get(pf, 0),
                "today_comments": today_comments_by_pf.get(pf, 0),
                "unread_dm": unread_by_pf.get(pf, 0),
                "active_tasks": 0,
                "failed_tasks": 0,
                "blocked_tasks": 0,
            }
            for pf in ALL_PLATFORMS
        }

        acc_map = {a.id: a for a in all_accounts if a.id is not None}
        for acc in all_accounts:
            entry = strip_map.get(acc.platform)
            if not entry:
                continue
            has_read, has_creator = _account_login_flags(acc)
            np = normalize_proxy(acc.proxy) if acc.proxy else ""
            ex_ip = str(acc.exit_ip or "").strip()
            shared_proxy = bool(
                (np and same_pf_proxy_counts.get((acc.platform, np), 0) > 1)
                or (ex_ip and same_pf_exit_counts.get((acc.platform, ex_ip), 0) > 1)
            )
            h_state = _account_health_state(
                acc,
                has_read_login=has_read,
                has_creator=has_creator,
                risk_state=risk_map.get(acc.id or 0),
                now=now,
                same_platform_direct_count=direct_by_pf.get(acc.platform, 0),
                same_platform_total_count=total_by_pf.get(acc.platform, 0),
                same_platform_shared_proxy=shared_proxy,
            )
            entry["accounts_total"] += 1
            if h_state == "invalid":
                entry["accounts_invalid"] += 1
            else:
                entry["accounts_active"] += 1
            if h_state == "risk":
                entry["accounts_risk"] += 1
            elif h_state == "proxy_issue":
                entry["accounts_proxy_issue"] += 1
            elif h_state == "shared_proxy":
                entry["accounts_shared_proxy"] += 1
            elif h_state == "no_proxy":
                entry["accounts_no_proxy"] += 1
            entry["followers_total"] += int(acc.follower_count or 0)
            entry["fans_delta"] += int(fans_deltas.get(acc.id or 0, 0))
            t_roll = rollups.get(acc.id or 0)
            if t_roll:
                entry["active_tasks"] += int(t_roll["pending"] + t_roll["running"])
                entry["failed_tasks"] += int(t_roll["failed"])
                entry["blocked_tasks"] += int(t_roll["blocked"])

        platform_strip = list(strip_map.values())
        scoped_entries = [e for e in platform_strip if not pf_filter or e["platform"] == pf_filter]

        alerts = {
            "invalid_accounts": sum(e["accounts_invalid"] for e in scoped_entries),
            "proxy_issues": sum(e["accounts_proxy_issue"] for e in scoped_entries),
            "shared_proxy_accounts": sum(e["accounts_shared_proxy"] for e in scoped_entries),
            "risk_accounts": sum(e["accounts_risk"] for e in scoped_entries),
            "no_proxy_accounts": sum(e["accounts_no_proxy"] for e in scoped_entries),
            "unread_dm": sum(e["unread_dm"] for e in scoped_entries),
            "failed_tasks": sum(e["failed_tasks"] for e in scoped_entries),
            "blocked_tasks": sum(e["blocked_tasks"] for e in scoped_entries),
            "active_tasks": sum(e["active_tasks"] for e in scoped_entries),
            "pending_reviews": pending_reviews,
            "abnormal_works": abnormal_works_scope,
        }

        totals = {
            "accounts": sum(e["accounts_total"] for e in scoped_entries),
            "active_accounts": sum(e["accounts_active"] for e in scoped_entries),
            "followers": sum(e["followers_total"] for e in scoped_entries),
            "fans_delta": sum(e["fans_delta"] for e in scoped_entries),
            "monitors": sum(e["monitors_enabled"] for e in scoped_entries),
            "contents_total": contents_total_scope,
            "downloaded_total": downloaded_total_scope,
            "comments_total": comments_total_scope,
            "own_works_total": own_works_scope,
            "today_contents": sum(e["today_contents"] for e in scoped_entries),
            "today_comments": sum(e["today_comments"] for e in scoped_entries),
        }

        # 最新/高赞捕获内容卡片流预览 (合并监控作品与关键词采集作品)
        c_stmt = select(ContentRecord)
        if pf_filter:
            c_stmt = c_stmt.where(ContentRecord.platform == pf_filter)
        recent_mon_items = session.exec(
            c_stmt.order_by(ContentRecord.created_at.desc()).limit(24)
        ).all()

        k_stmt = select(KeywordCollectionContent)
        if pf_filter:
            k_stmt = k_stmt.where(KeywordCollectionContent.platform == pf_filter)
        recent_col_items = session.exec(
            k_stmt.order_by(KeywordCollectionContent.created_at.desc()).limit(24)
        ).all()

        preview_pool: list[dict[str, Any]] = []
        for rec in recent_mon_items:
            has_cover = bool(thumb_cache.find_work_local_cover(rec.local_path, rec.aweme_id) or rec.cover_url)
            preview_pool.append({
                "uid": f"monitor:{rec.id}",
                "source_kind": "monitor",
                "id": rec.id,
                "platform": rec.platform,
                "aweme_id": rec.aweme_id,
                "desc": rec.desc or "（无作品描述）",
                "cover_url": f"/api/contents/{rec.id}/cover" if has_cover else "",
                "media_type": rec.media_type or "video",
                "like_count": int(rec.like_count or 0),
                "comment_count": int(rec.comment_count or 0),
                "author_name": mon_name_map.get(rec.target_id, f"监控 #{rec.target_id}"),
                "source_label": "作品监控",
                "download_status": rec.download_status or "pending",
                "local_ready": bool(rec.download_status == "done" and rec.local_path),
                "create_time": int(rec.create_time or 0),
                "captured_at": utc_iso(rec.created_at),
                "_sort_ts": rec.created_at.timestamp() if rec.created_at else 0,
            })
        for rec in recent_col_items:
            has_cover = bool(thumb_cache.find_work_local_cover(rec.local_path, rec.aweme_id) or rec.cover_url)
            preview_pool.append({
                "uid": f"collection:{rec.id}",
                "source_kind": "collection",
                "id": rec.id,
                "platform": rec.platform,
                "aweme_id": rec.aweme_id,
                "desc": rec.desc or "（无作品描述）",
                "cover_url": f"/api/collections/{rec.job_id}/contents/{rec.id}/cover" if has_cover else "",
                "media_type": rec.media_type or "video",
                "like_count": int(rec.like_count or 0),
                "comment_count": int(rec.comment_count or 0),
                "author_name": rec.author_name or (f"#{rec.keyword}" if rec.keyword else "批量采集"),
                "source_label": f"采集 · {rec.keyword}" if rec.keyword else "批量采集",
                "download_status": rec.download_status or "skipped",
                "local_ready": bool(rec.download_status == "done" and rec.local_path),
                "create_time": int(rec.create_time or 0),
                "captured_at": utc_iso(rec.created_at),
                "_sort_ts": rec.created_at.timestamp() if rec.created_at else 0,
            })
        preview_pool.sort(key=lambda item: (item["_sort_ts"], item["like_count"]), reverse=True)
        hot_contents = [{k: v for k, v in item.items() if k != "_sort_ts"} for item in preview_pool[:12]]

        # 发布排期与近期发布动态
        p_stmt = select(PublishTask)
        if pf_filter:
            p_stmt = p_stmt.where(PublishTask.platform == pf_filter)
        pub_rows = session.exec(p_stmt.order_by(PublishTask.id.desc()).limit(40)).all()
        schedule_items: list[dict[str, Any]] = []
        for task in pub_rows:
            is_active = task.status in {"draft", "pending", "publishing", "uncertain"}
            is_today = bool(
                (task.scheduled_at and task.scheduled_at >= today_start_utc)
                or (task.created_at and task.created_at >= today_start_utc)
                or (task.done_at and task.done_at >= today_start_utc)
            )
            if not (is_active or is_today) and len(schedule_items) >= 8:
                continue
            acc = acc_map.get(task.account_id or 0)
            schedule_items.append({
                "id": task.id,
                "platform": task.platform,
                "account_id": task.account_id,
                "account_name": _account_display_name(acc, task.account_id),
                "group_name": (acc.group_name if acc else "") or "",
                "title": task.title or (task.desc or "").strip()[:36] or "(未命名发布任务)",
                "media_type": task.media_type,
                "status": task.status,
                "scheduled_at": (
                    utc_iso(task.scheduled_at)
                    if task.scheduled_at_is_utc
                    else (task.scheduled_at.isoformat() if task.scheduled_at else None)
                ),
                "next_allowed_at": utc_iso(task.next_allowed_at),
                "blocked_reason": task.blocked_reason or "",
                "error": task.error or "",
                "created_at": utc_iso(task.created_at),
            })
            if len(schedule_items) >= 12:
                break

        return {
            "scope": pf_filter or "all",
            "platform_strip": platform_strip,
            "alerts": alerts,
            "totals": totals,
            "series_7d": {
                "days": day_labels,
                "contents": contents_7d,
                "comments": comments_7d,
                "publishes": publishes_7d,
            },
            "hot_contents": hot_contents,
            "schedule_items": schedule_items,
        }


# ─────────────────── 账号矩阵中心（全平台查询、分组管理与批量运维） ───────────────────

@router.get("/api/matrix/accounts")
async def list_matrix_accounts(platform: str = ""):
    pf_filter = _norm_platform(platform)
    now = datetime.utcnow()
    open_ids = set(login_sessions_service._active_open_browser_account_ids())

    with get_session() as session:
        all_accounts = session.exec(select(DouyinAccount)).all()
        direct_by_pf: dict[str, int] = {}
        total_by_pf: dict[str, int] = {}
        total_proxy_counts: dict[str, int] = {}
        same_pf_proxy_counts: dict[tuple[str, str], int] = {}
        same_pf_exit_counts: dict[tuple[str, str], int] = {}
        profile_counts: dict[str, int] = {}
        profile_keys: dict[int, str] = {}
        for acc in all_accounts:
            total_by_pf[acc.platform] = total_by_pf.get(acc.platform, 0) + 1
            if not acc.proxy:
                direct_by_pf[acc.platform] = direct_by_pf.get(acc.platform, 0) + 1
            else:
                np = normalize_proxy(acc.proxy)
                if np:
                    total_proxy_counts[np] = total_proxy_counts.get(np, 0) + 1
                    same_pf_proxy_counts[(acc.platform, np)] = same_pf_proxy_counts.get((acc.platform, np), 0) + 1
            ex_ip = str(acc.exit_ip or "").strip()
            if ex_ip:
                same_pf_exit_counts[(acc.platform, ex_ip)] = same_pf_exit_counts.get((acc.platform, ex_ip), 0) + 1
            if acc.id and acc.profile_dir:
                try:
                    norm_p = str(Path(acc.profile_dir).expanduser().resolve()).lower()
                except Exception:
                    norm_p = str(acc.profile_dir).lower()
                profile_keys[acc.id] = norm_p
                profile_counts[norm_p] = profile_counts.get(norm_p, 0) + 1

        target_accounts = [
            a for a in all_accounts if not pf_filter or a.platform == pf_filter
        ]
        target_accounts.sort(
            key=lambda item: (not bool(item.pinned), int(item.sort_order or 0), item.id or 0)
        )
        ids = [a.id for a in target_accounts if a.id is not None]

        risk_map = {
            r.account_id: r
            for r in (
                session.exec(select(AccountRiskState).where(AccountRiskState.account_id.in_(ids))).all()
                if ids
                else []
            )
        }
        fans_deltas = _compute_fans_deltas(session, ids)
        rollups = _compute_task_rollups(session, ids, now)

        mon_counts: dict[int, int] = {}
        for acc_id in session.exec(select(MonitorTarget.account_id)).all():
            if acc_id:
                mon_counts[acc_id] = mon_counts.get(acc_id, 0) + 1

        unread_map: dict[int, int] = {}
        for row in session.exec(select(DmConversation).where(DmConversation.unread_count > 0)).all():
            unread_map[row.account_id] = unread_map.get(row.account_id, 0) + int(row.unread_count or 0)

        zero_play_map: dict[int, int] = {}
        abnormal_work_map: dict[int, int] = {}
        for aid, zp, st_alert in session.exec(
            select(AccountWork.account_id, AccountWork.zero_play_alerted, AccountWork.status_alerted)
        ).all():
            if zp:
                zero_play_map[aid] = zero_play_map.get(aid, 0) + 1
            if zp or bool(st_alert):
                abnormal_work_map[aid] = abnormal_work_map.get(aid, 0) + 1

        running_workers = {
            aid
            for aid in session.exec(
                select(KeywordCollectionJob.account_id).where(KeywordCollectionJob.status == "running")
            ).all()
            if aid
        }

        group_rows = session.exec(select(MatrixGroup).order_by(MatrixGroup.sort_order, MatrixGroup.id)).all()
        group_meta_map = {
            g.name: {
                "id": g.id,
                "name": g.name,
                "color": g.color or "blue",
                "description": g.description or "",
                "preferred_country": g.preferred_country or "",
                "sort_order": int(g.sort_order or 0),
            }
            for g in group_rows
            if g.name
        }

        all_tags_set: set[str] = set()
        for acc in all_accounts:
            for t in _parse_account_tags(acc.tags):
                all_tags_set.add(t)
            if acc.group_name and acc.group_name not in group_meta_map:
                group_meta_map[acc.group_name] = {
                    "id": None,
                    "name": acc.group_name,
                    "color": "blue",
                    "description": "",
                    "preferred_country": "",
                    "sort_order": 999,
                }

        items: list[dict[str, Any]] = []
        for acc in target_accounts:
            aid = acc.id or 0
            has_read, has_creator = _account_login_flags(acc)
            r_state = risk_map.get(aid)
            np = normalize_proxy(acc.proxy) if acc.proxy else ""
            ex_ip = str(acc.exit_ip or "").strip()
            shared_proxy = bool(
                (np and same_pf_proxy_counts.get((acc.platform, np), 0) > 1)
                or (ex_ip and same_pf_exit_counts.get((acc.platform, ex_ip), 0) > 1)
            )
            h_state = _account_health_state(
                acc,
                has_read_login=has_read,
                has_creator=has_creator,
                risk_state=r_state,
                now=now,
                same_platform_direct_count=direct_by_pf.get(acc.platform, 0),
                same_platform_total_count=total_by_pf.get(acc.platform, 0),
                same_platform_shared_proxy=shared_proxy,
            )
            shared_count = max(
                same_pf_proxy_counts.get((acc.platform, np), 0) if np else 0,
                same_pf_exit_counts.get((acc.platform, ex_ip), 0) if ex_ip else 0,
            )
            cross_pf_shared = bool(
                np
                and total_proxy_counts.get(np, 0) > 1
                and same_pf_proxy_counts.get((acc.platform, np), 0) <= 1
            )
            proxy_warns = inspect_account_proxy_risk(
                session,
                platform=acc.platform,
                proxy_url=acc.proxy or "",
                exclude_account_id=aid,
                candidate_exit_ip=ex_ip,
            )
            tags = _parse_account_tags(acc.tags)
            p_key = profile_keys.get(aid, "")
            t_roll = rollups.get(aid, {"total": 0, "pending": 0, "running": 0, "blocked": 0, "failed": 0})
            items.append({
                "id": aid,
                "platform": acc.platform,
                "nickname": acc.nickname or "",
                "alias": acc.alias or "",
                "display_name": _account_display_name(acc, aid),
                "group_name": acc.group_name or "",
                "tags": tags,
                "account_role": acc.account_role or "hybrid",
                "pinned": bool(acc.pinned),
                "sort_order": int(acc.sort_order or 0),
                "douyin_id": acc.douyin_id or "",
                "sec_uid": acc.sec_uid or "",
                "avatar": acc.avatar or "",
                "status": acc.status,
                "health_state": h_state,
                "has_creator": has_creator,
                "has_read_login": has_read,
                "login_type": "cookie" if acc.cookie else "scan",
                "follower_count": int(acc.follower_count or 0),
                "following_count": int(acc.following_count or 0),
                "aweme_count": int(acc.aweme_count or 0),
                "total_favorited": int(acc.total_favorited or 0),
                "fans_delta": int(fans_deltas.get(aid, 0)),
                "has_proxy": bool(acc.proxy),
                "proxy": proxies_service._mask_proxy(acc.proxy),
                "proxy_status": acc.proxy_status or "unknown",
                "shared_proxy": shared_proxy,
                "shared_proxy_count": shared_count,
                "cross_platform_shared": cross_pf_shared,
                "proxy_warnings": proxy_warns,
                "exit_ip": acc.exit_ip or "",
                "exit_country": acc.exit_country or "",
                "fp_country": acc.fp_country or "",
                "fp_region": acc.fp_region or "",
                "fp_city": acc.fp_city or "",
                "ip_matches_exit": bool(
                    not (acc.fp_source_ip and acc.exit_ip) or acc.fp_source_ip == acc.exit_ip
                ),
                "browser_backend": acc.browser_backend or "default",
                "browser_runtime_id": acc.browser_runtime_id or "",
                "browser_open": aid in open_ids,
                "worker_running": aid in running_workers or int(t_roll.get("running", 0)) > 0,
                "profile_isolated": bool(p_key and profile_counts.get(p_key) == 1),
                "monitor_count": mon_counts.get(aid, 0),
                "unread_dm": unread_map.get(aid, 0),
                "zero_play_count": zero_play_map.get(aid, 0),
                "abnormal_work_count": abnormal_work_map.get(aid, 0),
                "risk_level": int(r_state.risk_level or 0) if r_state else 0,
                "risk_reason": (r_state.last_risk_reason if r_state else "") or acc.write_pause_reason or "",
                "cooldown_until": utc_iso(r_state.cooldown_until) if (r_state and r_state.cooldown_until) else None,
                "manual_review_required": bool(r_state.manual_review_required) if r_state else False,
                "tasks": t_roll,
                "created_at": utc_iso(acc.created_at),
            })

        return {
            "items": items,
            "groups": sorted(group_meta_map.values(), key=lambda g: (g["sort_order"], g["name"])),
            "all_tags": sorted(all_tags_set),
        }


class MatrixGroupSaveIn(BaseModel):
    name: str
    rename_to: str | None = None
    color: str = "blue"
    description: str = ""
    preferred_country: str = ""
    sort_order: int = 0


@router.post("/api/matrix/groups")
async def save_matrix_group(body: MatrixGroupSaveIn):
    name = str(body.name or "").strip()[:40]
    if not name:
        raise HTTPException(400, "请输入矩阵分组名称")
    target_name = str(body.rename_to or name).strip()[:40]
    if not target_name:
        raise HTTPException(400, "新分组名称不能为空")
    color = body.color if body.color in GROUP_COLORS else "blue"

    with get_session() as session:
        existing = session.exec(select(MatrixGroup).where(MatrixGroup.name == name)).first()
        if target_name != name:
            conflict = session.exec(select(MatrixGroup).where(MatrixGroup.name == target_name)).first()
            if conflict and (not existing or conflict.id != existing.id):
                raise HTTPException(409, f"分组「{target_name}」已存在")
            for acc in session.exec(select(DouyinAccount).where(DouyinAccount.group_name == name)).all():
                acc.group_name = target_name
                session.add(acc)
        if existing is None:
            existing = session.exec(select(MatrixGroup).where(MatrixGroup.name == target_name)).first()
        if existing is None:
            existing = MatrixGroup(
                name=target_name,
                color=color,
                description=str(body.description or "").strip()[:160],
                preferred_country=str(body.preferred_country or "").strip().upper()[:8],
                sort_order=int(body.sort_order or 0),
            )
        else:
            existing.name = target_name
            existing.color = color
            existing.description = str(body.description or "").strip()[:160]
            existing.preferred_country = str(body.preferred_country or "").strip().upper()[:8]
            existing.sort_order = int(body.sort_order or 0)
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return {
            "ok": True,
            "group": {
                "id": existing.id,
                "name": existing.name,
                "color": existing.color,
                "description": existing.description,
                "preferred_country": existing.preferred_country,
                "sort_order": existing.sort_order,
            },
        }


@router.delete("/api/matrix/groups/{group_name}")
async def delete_matrix_group(group_name: str, unassign_accounts: bool = True):
    clean = str(group_name or "").strip()
    if not clean:
        raise HTTPException(400, "分组名无效")
    with get_session() as session:
        row = session.exec(select(MatrixGroup).where(MatrixGroup.name == clean)).first()
        if row:
            session.delete(row)
        cleared = 0
        if unassign_accounts:
            for acc in session.exec(select(DouyinAccount).where(DouyinAccount.group_name == clean)).all():
                acc.group_name = ""
                session.add(acc)
                cleared += 1
        session.commit()
    return {"ok": True, "cleared_accounts": cleared}


class AccountBatchActionIn(BaseModel):
    account_ids: list[int]
    action: str  # set_group | set_role | add_tags | set_tags | clear_tags | set_pinned | set_browser_backend | assign_proxies | clear_proxy | close_browsers
    group_name: str = ""
    account_role: str = "hybrid"
    tags: list[str] | str = PydanticField(default_factory=list)
    pinned: bool = False
    browser_backend: str = "default"
    browser_runtime_id: str = ""
    overwrite_existing_proxy: bool = False


@router.post("/api/matrix/accounts/batch")
async def batch_operate_accounts(body: AccountBatchActionIn):
    ids = sorted({int(x) for x in (body.account_ids or []) if int(x) > 0})
    if not ids:
        raise HTTPException(400, "请至少勾选一个账号")
    act = str(body.action or "").strip().lower()

    if act in {"set_group", "set_role", "add_tags", "set_tags", "clear_tags", "set_pinned"}:
        updated = 0
        clean_group = str(body.group_name or "").strip()[:40]
        clean_role = str(body.account_role or "hybrid").strip().lower()
        if act == "set_role" and clean_role not in {"publish", "crawler", "hybrid"}:
            raise HTTPException(400, "账号角色取值无效")
        incoming_tags = _clean_account_tags(body.tags)
        with get_session() as session:
            rows = session.exec(select(DouyinAccount).where(DouyinAccount.id.in_(ids))).all()
            for acc in rows:
                if act == "set_group":
                    acc.group_name = clean_group
                elif act == "set_role":
                    acc.account_role = clean_role
                elif act == "set_pinned":
                    acc.pinned = bool(body.pinned)
                elif act == "clear_tags":
                    acc.tags = "[]"
                elif act == "set_tags":
                    acc.tags = json.dumps(incoming_tags, ensure_ascii=False)
                elif act == "add_tags":
                    merged = _clean_account_tags([*_parse_account_tags(acc.tags), *incoming_tags])
                    acc.tags = json.dumps(merged, ensure_ascii=False)
                session.add(acc)
                updated += 1
            session.commit()
        return {"ok": True, "updated": updated}

    if act == "set_browser_backend":
        if app_runtime.browser is None:
            raise HTTPException(503, "浏览器组件尚未就绪")
        requested = str(body.browser_backend or "default").strip().lower()
        runtime_id = str(body.browser_runtime_id or "").strip()
        if requested not in ACCOUNT_BROWSER_BACKENDS:
            raise HTTPException(400, "浏览器后端取值无效")
        status = browser_runtimes_service._browser_backend_status(requested, runtime_id)
        if not status["available"]:
            raise HTTPException(400, f"所选浏览器内核不可用：{status['detail']}")

        updated_ids: list[int] = []
        skipped_xhs = 0
        with get_session() as session:
            rows = session.exec(select(DouyinAccount).where(DouyinAccount.id.in_(ids))).all()
            for acc in rows:
                acc.browser_backend = requested
                acc.browser_runtime_id = runtime_id if requested == "fingerprint_chromium" else ""
                session.add(acc)
                if acc.id:
                    updated_ids.append(acc.id)
            session.commit()
        for aid in updated_ids:
            lease = app_runtime.open_browsers.pop(aid, None)
            if lease is not None:
                try:
                    await lease.close()
                except Exception:
                    pass
            await app_runtime.browser.close_context(aid)
        return {"ok": True, "updated": len(updated_ids), "skipped_xhs": skipped_xhs}

    if act == "assign_proxies":
        assigned = 0
        shared_assigned = 0
        unassigned = 0
        skipped_has_proxy = 0
        touched_ids: list[int] = []
        for aid in ids:
            with get_session() as session:
                acc = session.get(DouyinAccount, aid)
                if not acc:
                    continue
                if acc.proxy and not body.overwrite_existing_proxy:
                    skipped_has_proxy += 1
                    continue
                picked = assign_proxy_from_pool(session, app_runtime.cfg, platform=acc.platform)
                if not picked:
                    unassigned += 1
                    continue
                risk_warns = inspect_account_proxy_risk(
                    session, platform=acc.platform, proxy_url=picked, exclude_account_id=aid
                )
                if any("共用" in w or "复用" in w for w in risk_warns):
                    shared_assigned += 1
                pool_row = resolve_pool_proxy_row(session, picked)
                acc.proxy = picked
                acc.proxy_id = pool_row.id if pool_row else None
                acc.proxy_status = "bound"
                acc.exit_ip = ""
                acc.exit_country = ""
                acc.exit_asn = ""
                acc.exit_timezone = ""
                acc.exit_proxy_signature = ""
                acc.exit_checked_at = None
                session.add(acc)
                session.commit()
                assigned += 1
                touched_ids.append(aid)
            if app_runtime.browser and aid in touched_ids:
                await app_runtime.browser.close_context(aid)
        warning = (
            f"代理池节点少于账号数，其中 {shared_assigned} 个账号复用了已有代理（存在多号同 IP 关联风险，建议补充独立节点）"
            if shared_assigned > 0
            else ""
        )
        return {
            "ok": True,
            "assigned": assigned,
            "shared_assigned": shared_assigned,
            "unassigned": unassigned,
            "skipped_has_proxy": skipped_has_proxy,
            "warning": warning,
        }

    if act == "clear_proxy":
        cleared = 0
        cleared_ids: list[int] = []
        with get_session() as session:
            rows = session.exec(select(DouyinAccount).where(DouyinAccount.id.in_(ids))).all()
            for acc in rows:
                if acc.proxy:
                    acc.proxy = ""
                    acc.proxy_id = None
                    acc.proxy_status = "unknown"
                    acc.exit_ip = ""
                    acc.exit_country = ""
                    acc.exit_asn = ""
                    acc.exit_timezone = ""
                    acc.exit_proxy_signature = ""
                    acc.exit_checked_at = None
                    session.add(acc)
                    cleared += 1
                    if acc.id is not None:
                        cleared_ids.append(acc.id)
            session.commit()
        for aid in cleared_ids:
            if app_runtime.im_receiver:
                await app_runtime.im_receiver.stop_account(aid)
            if app_runtime.browser:
                await app_runtime.browser.close_context(aid)
        warning = (
            f"已清除 {cleared} 个账号的代理配置（后续将走本机直连；多号或海外平台请注意真实 IP 关联与网络风控风险）"
            if cleared > 0
            else ""
        )
        return {"ok": True, "updated": cleared, "warning": warning}

    if act == "close_browsers":
        closed = 0
        for aid in ids:
            lease = app_runtime.open_browsers.pop(aid, None)
            if lease is not None:
                try:
                    await lease.close()
                    closed += 1
                except Exception:
                    pass
            if app_runtime.browser is not None:
                try:
                    await app_runtime.browser.close_context(aid)
                except Exception:
                    pass
        return {"ok": True, "closed": closed}

    raise HTTPException(400, f"不支持的批量操作类型：{act}")


# ─────────────────── 自有矩阵跨号数据对比与作品大盘 ───────────────────

@router.get("/api/matrix/hub-analytics")
async def matrix_hub_analytics(
    platform: str = "",
    group_name: str = "",
    role: str = "",
    work_filter: str = "all",
    sort: str = "latest",
    q: str = "",
    page: int = 1,
    page_size: int = 24,
):
    pf_filter = _norm_platform(platform)
    clean_group = str(group_name or "").strip()
    clean_role = str(role or "").strip().lower()
    query_text = str(q or "").strip().lower()
    page = max(1, int(page or 1))
    page_size = max(1, min(120, int(page_size or 24)))
    seven_days_ago_ts = int((datetime.utcnow() - timedelta(days=7)).timestamp())

    with get_session() as session:
        stmt = select(DouyinAccount)
        if pf_filter:
            stmt = stmt.where(DouyinAccount.platform == pf_filter)
        if clean_group:
            if clean_group == "__ungrouped__":
                stmt = stmt.where(DouyinAccount.group_name == "")
            else:
                stmt = stmt.where(DouyinAccount.group_name == clean_group)
        if clean_role in {"publish", "crawler", "hybrid"}:
            stmt = stmt.where(DouyinAccount.account_role == clean_role)
        accounts = session.exec(stmt).all()
        if query_text:
            accounts = [
                a for a in accounts
                if query_text in f"{a.alias} {a.nickname} {a.douyin_id} {a.group_name}".lower()
                or not query_text
            ]

        acc_ids = [a.id for a in accounts if a.id is not None]
        acc_map = {a.id: a for a in accounts if a.id is not None}
        fans_deltas = _compute_fans_deltas(session, acc_ids)

        unread_map: dict[int, int] = {}
        if acc_ids:
            for row in session.exec(
                select(DmConversation).where(
                    DmConversation.account_id.in_(acc_ids),
                    DmConversation.unread_count > 0,
                )
            ).all():
                unread_map[row.account_id] = unread_map.get(row.account_id, 0) + int(row.unread_count or 0)

        all_works = (
            session.exec(select(AccountWork).where(AccountWork.account_id.in_(acc_ids))).all()
            if acc_ids
            else []
        )

        per_acc_stats: dict[int, dict[str, int]] = {
            aid: {
                "works_synced": 0,
                "recent_7d_works": 0,
                "plays": 0,
                "likes": 0,
                "comments": 0,
                "collects": 0,
                "zero_play": 0,
                "abnormal": 0,
            }
            for aid in acc_ids
        }
        for w in all_works:
            st = per_acc_stats.get(w.account_id)
            if not st:
                continue
            st["works_synced"] += 1
            if int(w.create_time or 0) >= seven_days_ago_ts:
                st["recent_7d_works"] += 1
            st["plays"] += int(w.play_count or 0)
            st["likes"] += int(w.like_count or 0)
            st["comments"] += int(w.comment_count or 0)
            st["collects"] += int(w.collect_count or 0)
            if w.zero_play_alerted:
                st["zero_play"] += 1
            if w.zero_play_alerted or bool(w.status_alerted):
                st["abnormal"] += 1

        account_rankings: list[dict[str, Any]] = []
        for acc in accounts:
            aid = acc.id or 0
            st = per_acc_stats.get(aid, {})
            account_rankings.append({
                "id": aid,
                "platform": acc.platform,
                "display_name": _account_display_name(acc, aid),
                "nickname": acc.nickname or "",
                "alias": acc.alias or "",
                "avatar": thumb_cache.local_thumb_api_url(acc.avatar, platform=acc.platform),
                "douyin_id": acc.douyin_id or "",
                "group_name": acc.group_name or "",
                "account_role": acc.account_role or "hybrid",
                "pinned": bool(acc.pinned),
                "status": acc.status,
                "follower_count": int(acc.follower_count or 0),
                "fans_delta": int(fans_deltas.get(aid, 0)),
                "aweme_count": max(int(acc.aweme_count or 0), int(st.get("works_synced", 0))),
                "works_synced": int(st.get("works_synced", 0)),
                "recent_7d_works": int(st.get("recent_7d_works", 0)),
                "plays_total": int(st.get("plays", 0)),
                "likes_total": int(st.get("likes", 0)),
                "comments_total": int(st.get("comments", 0)),
                "zero_play_count": int(st.get("zero_play", 0)),
                "abnormal_count": int(st.get("abnormal", 0)),
                "unread_dm": unread_map.get(aid, 0),
            })

        account_rankings.sort(
            key=lambda x: (not x["pinned"], -x["follower_count"], -x["likes_total"], x["id"])
        )

        # 过滤与排序跨账号作品墙
        filtered_works: list[AccountWork] = []
        for w in all_works:
            is_abn = bool(w.zero_play_alerted or w.status_alerted)
            if work_filter == "abnormal" and not is_abn:
                continue
            if work_filter == "zero_play" and not w.zero_play_alerted:
                continue
            filtered_works.append(w)

        if sort == "plays_desc":
            filtered_works.sort(key=lambda w: (int(w.play_count or 0), int(w.like_count or 0)), reverse=True)
        elif sort == "likes_desc":
            filtered_works.sort(key=lambda w: (int(w.like_count or 0), int(w.play_count or 0)), reverse=True)
        elif sort == "comments_desc":
            filtered_works.sort(key=lambda w: int(w.comment_count or 0), reverse=True)
        else:
            filtered_works.sort(key=lambda w: (int(w.create_time or 0), w.id or 0), reverse=True)

        total_works_matched = len(filtered_works)
        pages = max(1, (total_works_matched + page_size - 1) // page_size)
        page = min(page, pages)
        sliced = filtered_works[(page - 1) * page_size : page * page_size]

        works_out = []
        for w in sliced:
            acc = acc_map.get(w.account_id)
            works_out.append({
                "id": w.id,
                "platform": w.platform,
                "account_id": w.account_id,
                "account_name": _account_display_name(acc, w.account_id),
                "account_avatar": thumb_cache.local_thumb_api_url(acc.avatar, platform=acc.platform) if acc else "",
                "group_name": (acc.group_name if acc else "") or "",
                "item_id": w.item_id,
                "desc": w.desc or "（无标题描述）",
                "media_type": w.media_type or "video",
                "cover_url": thumb_cache.local_thumb_api_url(w.cover_url, platform=w.platform),
                "create_time": int(w.create_time or 0),
                "play_count": int(w.play_count or 0),
                "like_count": int(w.like_count or 0),
                "comment_count": int(w.comment_count or 0),
                "collect_count": int(w.collect_count or 0),
                "status": w.status or "",
                "zero_play_alerted": bool(w.zero_play_alerted),
                "status_alerted": w.status_alerted or "",
            })

        kpi = {
            "accounts_count": len(account_rankings),
            "followers_total": sum(x["follower_count"] for x in account_rankings),
            "fans_delta_total": sum(x["fans_delta"] for x in account_rankings),
            "works_total": sum(x["aweme_count"] for x in account_rankings),
            "plays_total": sum(x["plays_total"] for x in account_rankings),
            "likes_total": sum(x["likes_total"] for x in account_rankings),
            "comments_total": sum(x["comments_total"] for x in account_rankings),
            "abnormal_works_total": sum(x["abnormal_count"] for x in account_rankings),
            "unread_dm_total": sum(x["unread_dm"] for x in account_rankings),
        }

        return {
            "kpi": kpi,
            "accounts": account_rankings,
            "works": {
                "items": works_out,
                "total": total_works_matched,
                "page": page,
                "page_size": page_size,
                "pages": pages,
            },
        }


# ─────────────────── 统一内容素材画廊与右侧分栏快览 ───────────────────

def _resolve_record_local_files(source_kind: str, rec: Any) -> list[Path]:
    """安全解析数据库记录对应的本地媒体文件列表（视频或图集多图）。"""
    files: list[Path] = []
    if source_kind in {"monitor", "collection"}:
        raw = str(getattr(rec, "local_path", "") or "").strip()
        if not raw:
            return []
        try:
            p = Path(raw).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return []
        if p.is_file() and p.stat().st_size > 0:
            aweme_id = str(getattr(rec, "aweme_id", "") or "").strip()
            if getattr(rec, "media_type", "") == "images" and aweme_id and p.parent.is_dir():
                siblings = sorted(
                    f for f in p.parent.glob(f"{aweme_id}_*")
                    if f.is_file()
                    and ".cover." not in f.name.lower()
                    and f.suffix.lower() in MEDIA_EXTENSIONS
                    and f.stat().st_size > 0
                )
                if siblings:
                    return siblings
            return [p]
        if p.is_dir():
            aweme_id = str(getattr(rec, "aweme_id", "") or "").strip()
            pattern = f"{aweme_id}_*" if aweme_id else "*"
            for f in sorted(p.glob(pattern)):
                try:
                    if (
                        f.is_file()
                        and ".cover." not in f.name.lower()
                        and f.suffix.lower() in MEDIA_EXTENSIONS
                        and f.stat().st_size > 0
                    ):
                        files.append(f)
                except OSError:
                    continue
            return files
    elif source_kind == "share":
        try:
            raw_list = json.loads(getattr(rec, "files_json", "[]") or "[]")
        except Exception:
            raw_list = []
        if isinstance(raw_list, list):
            for entry in raw_list:
                candidate = entry.get("path") if isinstance(entry, dict) else entry
                if not candidate:
                    continue
                try:
                    p = Path(str(candidate)).expanduser().resolve(strict=True)
                    if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS and p.stat().st_size > 0:
                        files.append(p)
                except (OSError, RuntimeError):
                    continue
    return files


@router.get("/api/matrix/media-hub")
async def list_media_hub(
    platform: str = "",
    source_kind: str = "all",
    media_type: str = "",
    local_only: bool = False,
    min_likes: int = 0,
    q: str = "",
    sort: str = "latest",
    page: int = 1,
    page_size: int = 24,
):
    pf_filter = _norm_platform(platform)
    src = str(source_kind or "all").strip().lower()
    if src not in {"all", "monitor", "collection", "own", "share"}:
        src = "all"
    m_type = str(media_type or "").strip().lower()
    query_text = str(q or "").strip().lower()
    min_likes = max(0, int(min_likes or 0))
    page = max(1, int(page or 1))
    page_size = max(1, min(120, int(page_size or 24)))

    unified: list[dict[str, Any]] = []
    source_counts = {"monitor": 0, "collection": 0, "own": 0, "share": 0}

    with get_session() as session:
        mon_map = {}
        for m in session.exec(select(MonitorTarget)).all():
            if m.id is not None:
                base = f"#{m.keyword}" if m.target_kind == "keyword" else (m.nickname or (m.sec_uid or "")[:12])
                mon_map[m.id] = {
                    "label": f"{m.alias} · {base}" if m.alias else base,
                    "group_name": m.group_name or "",
                }
        acc_map = {a.id: a for a in session.exec(select(DouyinAccount)).all() if a.id is not None}

        # 1) 作品监控记录
        m_stmt = select(ContentRecord)
        if pf_filter:
            m_stmt = m_stmt.where(ContentRecord.platform == pf_filter)
        for r in session.exec(m_stmt).all():
            source_counts["monitor"] += 1
            if src not in {"all", "monitor"}:
                continue
            local_ready = bool(r.download_status == "done" and r.local_path)
            if local_only and not local_ready:
                continue
            if m_type in {"video", "images"} and r.media_type != m_type:
                continue
            if int(r.like_count or 0) < min_likes:
                continue
            src_info = mon_map.get(r.target_id, {"label": f"监控 #{r.target_id}", "group_name": ""})
            if query_text and query_text not in f"{r.desc} {r.aweme_id} {src_info['label']} {src_info['group_name']}".lower():
                continue
            unified.append({
                "uid": f"monitor:{r.id}",
                "source_kind": "monitor",
                "id": r.id,
                "platform": r.platform,
                "aweme_id": r.aweme_id,
                "desc": r.desc or "（无标题描述）",
                "author_name": src_info["label"],
                "group_name": src_info["group_name"],
                "source_label": "作品监控",
                "media_type": r.media_type or "video",
                "cover_url": (
                    f"/api/contents/{r.id}/cover"
                    if (thumb_cache.find_work_local_cover(r.local_path, r.aweme_id) or r.cover_url)
                    else ""
                ),
                "duration": int(r.duration or 0),
                "like_count": int(r.like_count or 0),
                "comment_count": int(r.comment_count or 0),
                "play_count": 0,
                "download_status": r.download_status or "pending",
                "local_ready": local_ready,
                "local_path": r.local_path or "",
                "create_time": int(r.create_time or 0),
                "captured_at": utc_iso(r.created_at),
                "_captured_ts": r.created_at.timestamp() if r.created_at else 0,
            })

        # 2) 批量关键词采集记录
        k_stmt = select(KeywordCollectionContent)
        if pf_filter:
            k_stmt = k_stmt.where(KeywordCollectionContent.platform == pf_filter)
        for r in session.exec(k_stmt).all():
            source_counts["collection"] += 1
            if src not in {"all", "collection"}:
                continue
            local_ready = bool(r.download_status == "done" and r.local_path)
            if local_only and not local_ready:
                continue
            if m_type in {"video", "images"} and r.media_type != m_type:
                continue
            if int(r.like_count or 0) < min_likes:
                continue
            kw_label = f"采集 · #{r.keyword}" if r.keyword else f"采集任务 #{r.job_id}"
            if query_text and query_text not in f"{r.desc} {r.aweme_id} {r.author_name} {r.keyword}".lower():
                continue
            unified.append({
                "uid": f"collection:{r.id}",
                "source_kind": "collection",
                "id": r.id,
                "platform": r.platform,
                "aweme_id": r.aweme_id,
                "desc": r.desc or "（无标题描述）",
                "author_name": r.author_name or kw_label,
                "group_name": r.keyword or "",
                "source_label": kw_label,
                "media_type": r.media_type or "video",
                "cover_url": (
                    f"/api/collections/{r.job_id}/contents/{r.id}/cover"
                    if (thumb_cache.find_work_local_cover(r.local_path, r.aweme_id) or r.cover_url)
                    else ""
                ),
                "duration": 0,
                "like_count": int(r.like_count or 0),
                "comment_count": int(r.comment_count or 0),
                "play_count": 0,
                "download_status": r.download_status or "skipped",
                "local_ready": local_ready,
                "local_path": r.local_path or "",
                "create_time": int(r.create_time or 0),
                "captured_at": utc_iso(r.created_at),
                "_captured_ts": r.created_at.timestamp() if r.created_at else 0,
            })

        # 3) 自有账号已发作品
        o_stmt = select(AccountWork)
        if pf_filter:
            o_stmt = o_stmt.where(AccountWork.platform == pf_filter)
        for r in session.exec(o_stmt).all():
            source_counts["own"] += 1
            if src not in {"all", "own"}:
                continue
            if local_only:
                continue
            if m_type in {"video", "images"} and r.media_type != m_type:
                continue
            if int(r.like_count or 0) < min_likes:
                continue
            acc = acc_map.get(r.account_id)
            acc_label = _account_display_name(acc, r.account_id)
            if query_text and query_text not in f"{r.desc} {r.item_id} {acc_label}".lower():
                continue
            unified.append({
                "uid": f"own:{r.id}",
                "source_kind": "own",
                "id": r.id,
                "platform": r.platform,
                "aweme_id": r.item_id,
                "desc": r.desc or "（无标题描述）",
                "author_name": acc_label,
                "group_name": (acc.group_name if acc else "") or "",
                "source_label": "自有账号作品",
                "media_type": r.media_type or "video",
                "cover_url": thumb_cache.local_thumb_api_url(r.cover_url, platform=r.platform),
                "duration": 0,
                "like_count": int(r.like_count or 0),
                "comment_count": int(r.comment_count or 0),
                "play_count": int(r.play_count or 0),
                "download_status": "own",
                "local_ready": False,
                "local_path": "",
                "create_time": int(r.create_time or 0),
                "captured_at": utc_iso(r.fetched_at or r.created_at),
                "_captured_ts": (r.fetched_at or r.created_at).timestamp() if (r.fetched_at or r.created_at) else 0,
            })

        # 4) 分享链接解析下载记录
        s_stmt = select(ShareDownloadRecord)
        if pf_filter:
            s_stmt = s_stmt.where(ShareDownloadRecord.platform == pf_filter)
        for r in session.exec(s_stmt).all():
            source_counts["share"] += 1
            if src not in {"all", "share"}:
                continue
            local_ready = r.status == "done"
            if local_only and not local_ready:
                continue
            norm_mtype = "images" if r.media_type in {"images", "image", "gallery"} else "video"
            if m_type in {"video", "images"} and norm_mtype != m_type:
                continue
            if min_likes > 0:
                continue
            if query_text and query_text not in f"{r.title} {r.author} {r.item_id}".lower():
                continue
            unified.append({
                "uid": f"share:{r.id}",
                "source_kind": "share",
                "id": r.id,
                "platform": r.platform if r.platform in ALL_PLATFORMS else "douyin",
                "aweme_id": r.item_id or "",
                "desc": r.title or "（分享链接下载素材）",
                "author_name": r.author or "分享链接解析",
                "group_name": "",
                "source_label": "链接下载",
                "media_type": norm_mtype,
                "cover_url": (
                    f"/api/share-download/history/{r.id}/cover"
                    if (share_downloads_service._find_share_local_cover(r) or r.cover_url)
                    else ""
                ),
                "duration": 0,
                "like_count": 0,
                "comment_count": 0,
                "play_count": 0,
                "download_status": r.status or "done",
                "local_ready": local_ready,
                "local_path": r.output_dir or "",
                "create_time": int(r.created_at.timestamp()) if r.created_at else 0,
                "captured_at": utc_iso(r.created_at),
                "_captured_ts": r.created_at.timestamp() if r.created_at else 0,
            })

    if sort == "likes_desc":
        unified.sort(key=lambda x: (x["like_count"], x["_captured_ts"]), reverse=True)
    elif sort == "comments_desc":
        unified.sort(key=lambda x: (x["comment_count"], x["like_count"]), reverse=True)
    elif sort == "publish_desc":
        unified.sort(key=lambda x: (x["create_time"], x["_captured_ts"]), reverse=True)
    else:
        unified.sort(key=lambda x: (x["_captured_ts"], x["like_count"]), reverse=True)

    total = len(unified)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    items = [
        {k: v for k, v in row.items() if k != "_captured_ts"}
        for row in unified[(page - 1) * page_size : page * page_size]
    ]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "source_counts": {
            "all": sum(source_counts.values()),
            **source_counts,
        },
    }


@router.get("/api/matrix/media-hub/detail")
async def media_hub_detail(source_kind: str, id: int):
    kind = str(source_kind or "").strip().lower()
    with get_session() as session:
        if kind == "monitor":
            rec = session.get(ContentRecord, id)
            if not rec:
                raise HTTPException(404, "记录不存在")
            try:
                remote_medias = json.loads(rec.media_json or "[]")
            except Exception:
                remote_medias = []
            local_files = _resolve_record_local_files("monitor", rec)
            platform = rec.platform
            aweme_id = rec.aweme_id
            desc = rec.desc or ""
            cover_url = (
                f"/api/contents/{rec.id}/cover"
                if (thumb_cache.find_work_local_cover(rec.local_path, rec.aweme_id) or rec.cover_url)
                else ""
            )
            media_type = rec.media_type or "video"
            local_path = rec.local_path or ""
            source_url = ""
            can_repost = bool(rec.download_status == "done" and local_files)
            like_count = int(rec.like_count or 0)
            comment_count = int(rec.comment_count or 0)
        elif kind == "collection":
            rec = session.get(KeywordCollectionContent, id)
            if not rec:
                raise HTTPException(404, "采集记录不存在")
            try:
                remote_medias = json.loads(rec.media_json or "[]")
            except Exception:
                remote_medias = []
            local_files = _resolve_record_local_files("collection", rec)
            platform = rec.platform
            aweme_id = rec.aweme_id
            desc = rec.desc or ""
            cover_url = (
                f"/api/collections/{rec.job_id}/contents/{rec.id}/cover"
                if (thumb_cache.find_work_local_cover(rec.local_path, rec.aweme_id) or rec.cover_url)
                else ""
            )
            media_type = rec.media_type or "video"
            local_path = rec.local_path or ""
            source_url = rec.source_url or ""
            can_repost = bool(local_files)
            like_count = int(rec.like_count or 0)
            comment_count = int(rec.comment_count or 0)
        elif kind == "own":
            rec = session.get(AccountWork, id)
            if not rec:
                raise HTTPException(404, "自有作品记录不存在")
            local_files = []
            platform = rec.platform
            aweme_id = rec.item_id
            desc = rec.desc or ""
            cover_url = thumb_cache.local_thumb_api_url(rec.cover_url, platform=rec.platform)
            remote_medias = [{"kind": "image", "url": cover_url}] if cover_url else []
            media_type = rec.media_type or "video"
            local_path = ""
            source_url = ""
            can_repost = False
            like_count = int(rec.like_count or 0)
            comment_count = int(rec.comment_count or 0)
        elif kind == "share":
            rec = session.get(ShareDownloadRecord, id)
            if not rec:
                raise HTTPException(404, "下载记录不存在")
            local_files = _resolve_record_local_files("share", rec)
            platform = rec.platform
            aweme_id = rec.item_id or ""
            desc = rec.title or ""
            cover_url = (
                f"/api/share-download/history/{rec.id}/cover"
                if (share_downloads_service._find_share_local_cover(rec) or rec.cover_url)
                else ""
            )
            remote_medias = [{"kind": "image", "url": cover_url}] if cover_url else []
            media_type = "images" if rec.media_type in {"images", "image", "gallery"} else "video"
            local_path = rec.output_dir or ""
            source_url = rec.source_url or ""
            can_repost = bool(local_files)
            like_count = 0
            comment_count = 0
        else:
            raise HTTPException(400, "未知内容来源类型")

        if isinstance(remote_medias, list):
            remote_medias = [
                {
                    **item,
                    "url": (
                        thumb_cache.local_thumb_api_url(str(item.get("url") or ""), platform=platform)
                        if item.get("kind") == "image"
                        else item.get("url", "")
                    ),
                }
                if isinstance(item, dict)
                else item
                for item in remote_medias
            ]

        local_medias = [
            {
                "kind": "video" if f.suffix.lower() in VIDEO_EXTENSIONS else "image",
                "url": f"/api/matrix/media-hub/local-file?source_kind={kind}&id={id}&index={idx}",
                "name": f.name,
                "local_path": str(f),
            }
            for idx, f in enumerate(local_files)
        ]

        # 聚合该作品在本地已抓到的高赞评论（含评论监控 + 关键词采集评论）
        comments_out: list[dict[str, Any]] = []
        seen_cids: set[str] = set()
        if aweme_id:
            if kind == "collection":
                col_cmts = session.exec(
                    select(KeywordCollectionComment)
                    .where(KeywordCollectionComment.content_id == id)
                    .order_by(KeywordCollectionComment.like_count.desc())
                    .limit(40)
                ).all()
                for c in col_cmts:
                    cid = c.comment_id or f"col:{c.id}"
                    if cid in seen_cids:
                        continue
                    seen_cids.add(cid)
                    comments_out.append({
                        "id": cid,
                        "user_nickname": c.user_nickname or "匿名用户",
                        "text": c.text or "",
                        "ip_label": c.ip_label or "",
                        "like_count": int(c.like_count or 0),
                        "create_time": int(c.create_time or 0),
                        "is_reply": bool(c.reply_to),
                    })
            mon_cmts = session.exec(
                select(CommentRecord)
                .where(CommentRecord.platform == platform, CommentRecord.aweme_id == aweme_id)
                .order_by(CommentRecord.like_count.desc())
                .limit(40)
            ).all()
            for c in mon_cmts:
                cid = c.comment_id or f"mon:{c.id}"
                if cid in seen_cids:
                    continue
                seen_cids.add(cid)
                comments_out.append({
                    "id": cid,
                    "user_nickname": c.user_nickname or "匿名用户",
                    "text": c.text or "",
                    "ip_label": c.ip_label or "",
                    "like_count": int(c.like_count or 0),
                    "create_time": int(c.create_time or 0),
                    "is_reply": bool(c.reply_to),
                })
        comments_out.sort(key=lambda x: x["like_count"], reverse=True)

        return {
            "source_kind": kind,
            "id": id,
            "platform": platform,
            "aweme_id": aweme_id,
            "desc": desc,
            "cover_url": cover_url,
            "media_type": media_type,
            "like_count": like_count,
            "comment_count": comment_count,
            "local_path": local_path,
            "local_file_paths": [str(f) for f in local_files],
            "local_medias": local_medias,
            "remote_medias": remote_medias if isinstance(remote_medias, list) else [],
            "source_url": source_url,
            "can_repost": can_repost,
            "comments": comments_out[:30],
        }


@router.api_route("/api/matrix/media-hub/local-file", methods=["GET", "HEAD"])
async def serve_media_hub_local_file(source_kind: str, id: int, index: int = 0):
    kind = str(source_kind or "").strip().lower()
    with get_session() as session:
        rec = (
            session.get(ContentRecord, id)
            if kind == "monitor"
            else session.get(KeywordCollectionContent, id)
            if kind == "collection"
            else session.get(ShareDownloadRecord, id)
            if kind == "share"
            else None
        )
        if not rec:
            raise HTTPException(404, "记录不存在")
        files = _resolve_record_local_files(kind, rec)
    if index < 0 or index >= len(files):
        raise HTTPException(404, "本地媒体文件不存在")
    target = files[index]
    return FileResponse(
        target,
        filename=target.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=86400"},
    )


class MediaRevealIn(BaseModel):
    source_kind: str
    id: int


@router.post("/api/matrix/media-hub/reveal")
async def reveal_media_hub_file(body: MediaRevealIn, request: Request):
    share_downloads_service._require_local_action(request)
    kind = str(body.source_kind or "").strip().lower()
    with get_session() as session:
        rec = (
            session.get(ContentRecord, body.id)
            if kind == "monitor"
            else session.get(KeywordCollectionContent, body.id)
            if kind == "collection"
            else session.get(ShareDownloadRecord, body.id)
            if kind == "share"
            else None
        )
        if not rec:
            raise HTTPException(404, "记录不存在")
        files = _resolve_record_local_files(kind, rec)
        fallback_raw = getattr(rec, "local_path", "") or getattr(rec, "output_dir", "")
    target: Path | None = files[0] if files else None
    if target is None and fallback_raw:
        try:
            p = Path(str(fallback_raw)).expanduser().resolve(strict=True)
            if p.exists():
                target = p
        except (OSError, RuntimeError):
            target = None
    if target is None:
        raise HTTPException(404, "本地文件或目录不存在")
    try:
        await asyncio.to_thread(contents_service._reveal_in_file_manager, target)
    except OSError as exc:
        raise HTTPException(500, f"打开文件夹失败：{exc}") from exc
    return {"ok": True}


# ─────────────────── 跨平台多账号矩阵错峰群发 ───────────────────

class MatrixPublishOverrideIn(BaseModel):
    title: str | None = None
    desc: str | None = None
    topics: str | None = None
    visibility: str = "public"
    bili_options: dict[str, Any] = PydanticField(default_factory=dict)
    ig_options: dict[str, Any] = PydanticField(default_factory=dict)
    tw_options: dict[str, Any] = PydanticField(default_factory=dict)


class MatrixBatchPublishIn(BaseModel):
    account_ids: list[int]
    media_type: str = "video"               # video | images
    media_paths: list[str] = PydanticField(default_factory=list)
    title: str = ""
    desc: str = ""
    topics: str = ""
    location: str = ""
    scheduled_at: str | None = None
    stagger_minutes: int = 0                # >0 时每个账号顺延错峰 N 分钟
    source_platform: str = ""
    source_content_id: int | None = None
    platform_overrides: dict[str, MatrixPublishOverrideIn] = PydanticField(default_factory=dict)


@router.post("/api/matrix/publish-batch")
async def create_matrix_batch_publish(body: MatrixBatchPublishIn):
    target_ids = [int(x) for x in (body.account_ids or []) if int(x) > 0]
    seen_ids: set[int] = set()
    ordered_ids: list[int] = []
    for x in target_ids:
        if x not in seen_ids:
            seen_ids.add(x)
            ordered_ids.append(x)
    if not ordered_ids:
        raise HTTPException(400, "请至少勾选一个要发布的账号")
    if body.media_type not in {"video", "images"}:
        raise HTTPException(400, "media_type 须为 video 或 images")

    stagger = max(0, min(1440, int(body.stagger_minutes or 0)))
    created_tasks: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    with get_session() as session:
        for idx, aid in enumerate(ordered_ids):
            acc = session.get(DouyinAccount, aid)
            if not acc or acc.platform not in ALL_PLATFORMS:
                skipped.append({"account_id": aid, "reason": "账号不存在或平台不支持"})
                continue
            if str(acc.account_role or "hybrid").strip().lower() == "crawler":
                skipped.append({
                    "account_id": aid,
                    "account_name": _account_display_name(acc, aid),
                    "platform": acc.platform,
                    "reason": "账号角色为「仅采集号 (crawler)」，已禁止执行作品发布",
                })
                continue
            has_read, has_creator = _account_login_flags(acc)
            if acc.status == "invalid" or (acc.platform == "xhs" and not has_creator) or (
                acc.platform != "xhs" and not (has_creator or has_read or acc.cookie)
            ):
                skipped.append({
                    "account_id": aid,
                    "account_name": _account_display_name(acc, aid),
                    "platform": acc.platform,
                    "reason": "账号登录态失效或未完成创作者授权",
                })
                continue

            ov = body.platform_overrides.get(acc.platform)
            eff_title = (ov.title if (ov and ov.title is not None) else body.title).strip()
            eff_desc = ov.desc if (ov and ov.desc is not None) else body.desc
            eff_topics = (ov.topics if (ov and ov.topics is not None) else body.topics).strip()
            eff_vis = (ov.visibility if ov else "public")
            if eff_vis not in {"public", "friends", "private"}:
                eff_vis = "public"

            # 校验该平台媒体合法性（Twitter 图集若超过 4 张自动截取前 4 张并提示，不中断整批任务）
            candidate_paths = list(body.media_paths or [])
            if acc.platform == "twitter" and body.media_type == "images" and len(candidate_paths) > 4:
                candidate_paths = candidate_paths[:4]
            if acc.platform == "instagram" and len(candidate_paths) > 10:
                candidate_paths = candidate_paths[:10]

            try:
                if acc.platform in {"xhs", "rednote"}:
                    from app.platforms.xhs.media import validate_publish_files
                    paths = validate_publish_files(body.media_type, candidate_paths)
                else:
                    paths = _validate_non_xhs_publish_files(acc.platform, body.media_type, candidate_paths)
                    if acc.platform == "twitter" and not paths and not (eff_title or eff_desc.strip()):
                        raise ValueError("Twitter 发布需填写文本或上传媒体")
            except (HTTPException, ValueError) as exc:
                reason = exc.detail if isinstance(exc, HTTPException) else str(exc)
                skipped.append({
                    "account_id": aid,
                    "account_name": _account_display_name(acc, aid),
                    "platform": acc.platform,
                    "reason": str(reason),
                })
                continue

            title_cap = _publish_title_cap(acc.platform)
            norm_title = (
                weighted_truncate(eff_title, 20)
                if acc.platform == "rednote"
                else eff_title[:title_cap]
            )

            base_when = metadata_service._parse_when(body.scheduled_at, acc.timezone_id)
            if stagger > 0 and len(created_tasks) > 0:
                anchor = base_when or datetime.utcnow()
                sched_time = anchor + timedelta(minutes=stagger * len(created_tasks))
            else:
                sched_time = base_when

            task = PublishTask(
                platform=acc.platform,
                account_id=aid,
                media_type=body.media_type,
                title=norm_title,
                desc=eff_desc,
                topics=eff_topics,
                location=(body.location or "").strip()[:60],
                visibility=eff_vis,
                media_json=json.dumps(paths),
                bili_options_json=json.dumps((ov.bili_options if ov else {}) or {}, ensure_ascii=False),
                ig_options_json=json.dumps((ov.ig_options if ov else {}) or {}, ensure_ascii=False),
                tw_options_json=json.dumps((ov.tw_options if ov else {}) or {}, ensure_ascii=False),
                scheduled_at=sched_time,
                scheduled_at_is_utc=True,
                source_platform=body.source_platform or "",
                source_content_id=body.source_content_id,
            )
            session.add(task)
            session.flush()
            created_tasks.append(_publish_dict(task))

        if created_tasks:
            session.commit()

    if not created_tasks and skipped:
        first_reason = skipped[0].get("reason") or "所选账号不满足该媒体类型的发布要求"
        raise HTTPException(422, f"未能创建发布任务：{first_reason}")

    return {
        "ok": True,
        "created_count": len(created_tasks),
        "skipped_count": len(skipped),
        "tasks": created_tasks,
        "skipped": skipped,
    }
