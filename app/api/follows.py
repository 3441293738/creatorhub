"""账号关注/粉丝列表查询与后台异步关注同步任务接口。"""
from __future__ import annotations

from datetime import datetime
import asyncio
import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import delete, func, or_
from sqlmodel import select

from app import runtime as app_runtime
from app.browser import fetch_follows
from app.db import get_session
from app.models import DouyinAccount, FollowEdge
from app.platforms.bilibili import BilibiliClient
from app.platforms.instagram import InstagramClient
from app.risk import OperationKind
from app.transport_matrix import resolve_transport

router = APIRouter()


# ─────────── 本账号管理:关注 / 粉丝 ───────────
def _follow_dict(f: FollowEdge) -> dict:
    return {
        "id": f.id, "platform": f.platform, "account_id": f.account_id,
        "direction": f.direction, "uid": f.uid, "sec_uid": f.sec_uid,
        "nickname": f.nickname, "avatar": f.avatar, "signature": f.signature,
        "is_mutual": f.is_mutual, "is_following": f.is_following,
        "fetched_at": f.fetched_at.isoformat() if f.fetched_at else None,
    }


@router.get("/api/follows")
async def list_follows(account_id: int, direction: str = "following",
                       page: int = 1, page_size: int = 50,
                       query: str = ""):
    if direction not in ("following", "fan"):
        raise HTTPException(400, "direction 仅支持 following | fan")
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 50), 200))
    query = " ".join(str(query or "").strip().split())[:100]
    filters = [FollowEdge.account_id == account_id,
               FollowEdge.direction == direction]
    if query:
        match = f"%{query}%"
        filters.append(or_(FollowEdge.nickname.like(match),
                           FollowEdge.uid.like(match),
                           FollowEdge.sec_uid.like(match),
                           FollowEdge.signature.like(match)))
    with get_session() as s:
        total = int(s.exec(select(func.count(FollowEdge.id)).where(
            *filters)).one() or 0)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        rows = s.exec(
            select(FollowEdge).where(*filters)
            .order_by(FollowEdge.id.desc())
            .offset((page - 1) * page_size).limit(page_size)).all()
        synced_at = s.exec(select(func.max(FollowEdge.fetched_at)).where(
            FollowEdge.account_id == account_id,
            FollowEdge.direction == direction)).one()
    return {
        "items": [_follow_dict(row) for row in rows],
        "total": total, "page": page, "page_size": page_size,
        "pages": pages,
        "synced_at": synced_at.isoformat() if synced_at else None,
    }


@router.get("/api/follows/{edge_id}")
async def get_follow(edge_id: int, account_id: int):
    with get_session() as s:
        row = s.get(FollowEdge, edge_id)
        if not row or row.account_id != account_id:
            raise HTTPException(404, "关注关系不存在")
        return _follow_dict(row)


def _replace_follow_snapshot(account_id: int, platform: str, direction: str,
                             users: list[dict], now: datetime,
                             progress=None) -> tuple[int, int]:
    incoming: dict[str, dict] = {}
    for user in users:
        uid = str(user.get("uid") or "")
        if uid:
            incoming[uid] = user
    with get_session() as s:
        known = set(s.exec(select(FollowEdge.uid).where(
            FollowEdge.account_id == account_id,
            FollowEdge.direction == direction)).all())
        # Delete + insert stays in one transaction: readers keep seeing the old
        # snapshot until the complete replacement commits, and failures roll back.
        s.execute(delete(FollowEdge).where(
            FollowEdge.account_id == account_id,
            FollowEdge.direction == direction))
        mappings = []
        total = len(incoming)
        for index, (uid, user) in enumerate(incoming.items(), 1):
            mappings.append({
                "platform": platform, "account_id": account_id,
                "direction": direction, "uid": uid,
                "sec_uid": str(user.get("sec_uid") or ""),
                "nickname": str(user.get("nickname") or ""),
                "avatar": str(user.get("avatar") or ""),
                "signature": str(user.get("signature") or ""),
                "is_mutual": bool(user.get("is_mutual")),
                "is_following": bool(user.get("is_following")),
                "raw_json": str(user.get("raw_json") or ""),
                "fetched_at": now, "created_at": now,
            })
            if len(mappings) >= 1000 or index == total:
                s.bulk_insert_mappings(FollowEdge, mappings)
                mappings.clear()
                if progress is not None:
                    progress({"phase": "saving", "saved": index,
                              "fetched": total})
        s.commit()
    return sum(1 for uid in incoming if uid not in known), total


async def _sync_follows_impl(account_id: int, direction: str = "following",
                             progress=None):
    if direction not in ("following", "fan"):
        raise HTTPException(400, "direction 仅支持 following | fan")

    def report(**changes):
        if progress is not None:
            progress(changes)

    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        platform = acc.platform
        uid = acc.sec_uid or ""
        operation = "following_list" if direction == "following" else "followers_list"
        transport = (
            resolve_transport(app_runtime.cfg, platform, operation, acc)
            if platform in {"douyin", "bilibili", "instagram"}
            else None
        )
        effective_mode = (transport["effective_mode"] if transport else "browser")
        if effective_mode == "unavailable":
            raise HTTPException(
                409,
                f"当前读取方式与{('关注' if direction == 'following' else '粉丝')}列表不兼容："
                f"{transport.get('reason') or transport.get('note') or '该通道不可用'}")
        if app_runtime.browser is None and effective_mode in {"browser"}:
            raise HTTPException(503, "浏览器未就绪")
        if app_runtime.browser is None and effective_mode == "hybrid" and platform not in {"bilibili", "instagram"}:
            raise HTTPException(503, "浏览器未就绪")
        if platform == "douyin" and effective_mode in {"api", "hybrid"} \
                and app_runtime.engine is None:
            raise HTTPException(503, "抖音 API 读取引擎未就绪")
        identity = (
            None
            if platform in {"douyin", "bilibili", "instagram"} and effective_mode == "api"
            else (
                app_runtime.browser.identity_for(acc)
                if callable(getattr(app_runtime.browser, "identity_for", None))
                else None
            )
        )
        bili_state = (acc.storage_state or acc.cookie or "") if platform == "bilibili" else ""
        bili_ua = (acc.ua or app_runtime.cfg.engine.user_agent) if platform == "bilibili" else ""
        bili_proxy = (acc.proxy or "") if platform == "bilibili" else ""
        bili_mid = (acc.uid or acc.sec_uid or "") if platform == "bilibili" else ""
        ig_state = (acc.storage_state or acc.creator_storage_state or acc.cookie or "") if platform == "instagram" else ""
        ig_ua = (acc.ua or app_runtime.cfg.engine.user_agent) if platform == "instagram" else ""
        ig_proxy = (acc.proxy or "") if platform == "instagram" else ""
        ig_uid = (acc.uid or acc.sec_uid or acc.douyin_id or "") if platform == "instagram" else ""
        ig_seed = (acc.fp_seed or acc.profile_dir or "") if platform == "instagram" else ""
        ig_tz = (acc.timezone_id or "Asia/Shanghai") if platform == "instagram" else "Asia/Shanghai"
        known: set[str] = set()
        existing_count = int(s.exec(select(func.count(FollowEdge.id)).where(
            FollowEdge.account_id == account_id,
            FollowEdge.direction == direction)).one() or 0)
    # API-only/browser-only are hard routing decisions. Hybrid's API attempt and
    # page fallback share one READ_HEAVY guard so the fallback is not blocked by
    # the API attempt's own minimum interval.
    users, err = [], ""
    source = "api" if effective_mode == "api" else "browser"
    capped = False

    async def _fetch_follow_snapshot():
        nonlocal source, capped
        direct_users: list[dict] = []
        direct_err = ""
        if platform == "douyin" and effective_mode in {"api", "hybrid"}:
            report(phase="fetching", source="api")
            try:
                if progress is None:
                    direct_users, direct_err = (
                        await app_runtime.engine._fetch_douyin_follows_direct_locked(
                            account_id, direction))
                else:
                    def direct_progress(update):
                        report(phase="fetching", source="api", **update)

                    direct_users, direct_err = (
                        await app_runtime.engine._fetch_douyin_follows_direct_locked(
                            account_id, direction, progress=direct_progress))
            except Exception as e:
                direct_users, direct_err = [], repr(e)
            # “empty”表示结构有效的空列表；empty_body/网络/半途停滞等错误不是有效快照。
            # 若大粉丝量翻过深水区触及平台可见上限(platform_cap)，且不少于库内已有条数，直接采纳。
            if (direct_users and not direct_err) or direct_err == "empty":
                source = "api"
                return direct_users, direct_err
            if (direct_err.startswith("platform_cap:") and direct_users
                    and len(direct_users) >= existing_count):
                source = "api"
                capped = True
                return direct_users, ""
            if effective_mode == "api" or direct_err.startswith("page_limit:"):
                source = "api"
                return direct_users, direct_err or "empty_response"
            print(f"[follow] douyin direct 未完成({direct_err}, got={len(direct_users)}),回退浏览器拦截")
        elif platform == "bilibili" and effective_mode in {"api", "hybrid"} and bili_state:
            report(phase="fetching", source="api")
            bcli = BilibiliClient(
                bili_state,
                bili_ua,
                proxy=bili_proxy,
                timeout=app_runtime.cfg.engine.request_timeout_seconds,
            )
            kind_b = "followers" if direction == "fan" else "following"
            b_users: list[dict] = []
            b_seen: set[str] = set()
            async with bcli.session_scope():
                t_mid = (bili_mid or bcli.self_mid or "").strip()
                if not t_mid:
                    prof, perr = await bcli.fetch_self_profile()
                    if perr:
                        return [], perr
                    t_mid = str(prof.get("uid") or prof.get("sec_uid") or "")
                if not t_mid:
                    return [], "missing_uid:账号缺自身 uid,请先点账号「刷新资料」"
                for pn in range(1, 11):
                    batch, has_more, berr = await bcli.fetch_follows(
                        t_mid, kind=kind_b, pn=pn, ps=50
                    )
                    if berr:
                        direct_err = berr
                        break
                    for u in batch:
                        u_id = str(u.get("uid") or "")
                        if u_id and u_id not in b_seen:
                            b_seen.add(u_id)
                            b_users.append(u)
                    report(phase="fetching", source="api", pages=pn, fetched=len(b_users))
                    if not has_more:
                        break
            if b_users or not direct_err:
                source = "api"
                return b_users, ("" if b_users else "empty")
            if effective_mode == "api" or direct_err.startswith("logged_out"):
                source = "api"
                return [], direct_err
        elif platform == "instagram" and effective_mode in {"api", "hybrid"} and ig_state:
            report(phase="fetching", source="api")
            icli = InstagramClient(
                ig_state,
                ig_ua,
                proxy=ig_proxy,
                timeout=app_runtime.cfg.engine.request_timeout_seconds,
                account_seed=ig_seed,
                timezone_id=ig_tz,
            )
            async with icli.session_scope():
                target_u = (ig_uid or icli.self_uid or "").strip()
                if not target_u:
                    prof, perr = await icli.fetch_self_profile()
                    if perr:
                        return [], perr
                    target_u = str(prof.get("uid") or prof.get("unique_id") or "")
                if not target_u:
                    return [], "missing_uid:账号缺自身 uid,请先点账号「刷新资料」"
                if direction == "fan":
                    ig_users, direct_err = await icli.fetch_followers(target_u, max_count=200)
                else:
                    ig_users, direct_err = await icli.fetch_following(target_u, max_count=200)
            if ig_users or not direct_err:
                source = "api"
                return ig_users, ("" if ig_users else "empty")
            if effective_mode == "api" or direct_err.startswith(("logged_out", "verification_required")) or app_runtime.browser is None:
                source = "api"
                return [], direct_err

        source = "browser_fallback" if effective_mode == "hybrid" else "browser"
        report(phase="fetching", source=source, pages=0, fetched=0)
        if progress is None:
            browser_users, browser_err = await fetch_follows(
                app_runtime.browser, identity, platform, uid, direction, known)
        else:
            def browser_progress(update):
                report(phase="fetching", source=source, **update)

            browser_users, browser_err = await fetch_follows(
                app_runtime.browser, identity, platform, uid, direction, known,
                progress=browser_progress)
        if browser_users and not browser_err:
            if direct_users and len(browser_users) < len(direct_users):
                return [], (
                    direct_err
                    or f"browser_truncated:got={len(browser_users)},direct={len(direct_users)}"
                )
            return browser_users, ""
        return [], (browser_err or direct_err)

    if app_runtime.engine is not None:
        users, err = await app_runtime.engine.guarded_read_pair(
            account_id, OperationKind.READ_HEAVY,
            f"follows-sync:{account_id}:{direction}",
            _fetch_follow_snapshot, empty_result=[])
    else:
        users, err = await _fetch_follow_snapshot()

    if err.startswith("risk_deferred:"):
        return {"ok": True, "fetched": 0, "added": 0,
                "skipped": True, "reason": err.split(":", 1)[-1],
                "source": "deferred",
                "configured_mode": (transport["configured_mode"]
                                    if transport else "browser")}
    if err and err.startswith("missing_uid"):
        raise HTTPException(400, err.split(":", 1)[-1])
    if err and err.startswith("logged_out"):
        raise HTTPException(400, "登录态已失效,请点「重新登录」")
    # 只有明确解析到平台的有效空列表("empty")才允许清空旧快照。超时、空 body、
    # 未命中接口等不确定结果必须保留旧数据，并把真实失败显示给前端。
    if err and err != "empty":
        label = "关注" if direction == "following" else "粉丝"
        raise HTTPException(502, f"{label}同步未取得有效列表，已保留原数据：{err}")
    now = datetime.utcnow()
    report(phase="saving", fetched=len(users), saved=0)
    added, stored = await asyncio.to_thread(
        _replace_follow_snapshot, account_id, platform, direction,
        users, now, progress)
    return {"ok": True, "fetched": len(users), "stored": stored,
            "added": added, "complete": True, "capped": capped,
            "source": source,
            "configured_mode": (transport["configured_mode"]
                                 if transport else "browser")}


@router.post("/api/accounts/{account_id}/follows/sync")
async def sync_follows(account_id: int, direction: str = "following"):
    return await _sync_follows_impl(account_id, direction)


def _follow_sync_job_view(job: dict) -> dict:
    view = dict(job)
    expected = max(0, int(view.get("expected_total") or 0))
    fetched = max(0, int(view.get("fetched") or 0))
    if view.get("status") == "completed":
        percent = 100
    elif expected:
        percent = min(99, int(fetched * 100 / expected))
    else:
        percent = None
    view["percent"] = percent
    view["cancelable"] = (view.get("status") in {"queued", "running"}
                          and view.get("phase") != "saving")
    return view


async def _run_follow_sync_job(job_id: str):
    job = app_runtime._follow_sync_jobs[job_id]

    def update(changes: dict):
        job.update(changes)
        job["updated_at"] = datetime.utcnow().isoformat()

    update({"status": "running", "phase": "fetching"})
    try:
        result = await _sync_follows_impl(
            int(job["account_id"]), str(job["direction"]), progress=update)
        if result.get("skipped"):
            update({"status": "deferred", "phase": "deferred",
                    "error": str(result.get("reason") or "同步暂缓"),
                    "result": result,
                    "completed_at": datetime.utcnow().isoformat()})
            return
        update({"status": "completed", "phase": "completed",
                "fetched": int(result.get("fetched") or 0),
                "saved": int(result.get("stored") or 0),
                "added": int(result.get("added") or 0),
                "capped": bool(result.get("capped")),
                "source": result.get("source") or "",
                "result": result, "completed_at": datetime.utcnow().isoformat()})
    except asyncio.CancelledError:
        update({"status": "canceled", "phase": "canceled",
                "completed_at": datetime.utcnow().isoformat()})
    except HTTPException as exc:
        update({"status": "failed", "phase": "failed",
                "error": str(exc.detail),
                "completed_at": datetime.utcnow().isoformat()})
    except Exception as exc:
        update({"status": "failed", "phase": "failed",
                "error": repr(exc),
                "completed_at": datetime.utcnow().isoformat()})


@router.post("/api/accounts/{account_id}/follows/sync-jobs", status_code=202)
async def start_follow_sync_job(account_id: int,
                                direction: str = "following"):
    if direction not in ("following", "fan"):
        raise HTTPException(400, "direction 仅支持 following | fan")
    with get_session() as s:
        account = s.get(DouyinAccount, account_id)
        if not account:
            raise HTTPException(404, "账号不存在")
        expected_total = (account.following_count if direction == "following"
                          else account.follower_count)
    for existing in reversed(list(app_runtime._follow_sync_jobs.values())):
        if (existing.get("account_id") == account_id
                and existing.get("direction") == direction
                and existing.get("status") in {"queued", "running"}):
            return _follow_sync_job_view(existing)
    completed = [key for key, value in app_runtime._follow_sync_jobs.items()
                 if value.get("status") in {"completed", "failed", "canceled"}]
    for key in completed[:-100]:
        app_runtime._follow_sync_jobs.pop(key, None)
        app_runtime._follow_sync_tasks.pop(key, None)
    now = datetime.utcnow().isoformat()
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id, "account_id": account_id, "direction": direction,
        "status": "queued", "phase": "queued", "pages": 0,
        "fetched": 0, "saved": 0, "added": 0,
        "expected_total": max(0, int(expected_total or 0)),
        "source": "", "error": "", "created_at": now,
        "updated_at": now, "completed_at": None,
    }
    app_runtime._follow_sync_jobs[job_id] = job
    task = asyncio.create_task(_run_follow_sync_job(job_id))
    app_runtime._follow_sync_tasks[job_id] = task
    task.add_done_callback(lambda _task, key=job_id:
                           app_runtime._follow_sync_tasks.pop(key, None))
    return _follow_sync_job_view(job)


@router.get("/api/follow-sync-jobs/{job_id}")
async def get_follow_sync_job(job_id: str):
    job = app_runtime._follow_sync_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "同步任务不存在")
    return _follow_sync_job_view(job)


@router.get("/api/accounts/{account_id}/follows/sync-job")
async def get_latest_follow_sync_job(account_id: int,
                                     direction: str = "following"):
    for job in reversed(list(app_runtime._follow_sync_jobs.values())):
        if (job.get("account_id") == account_id
                and job.get("direction") == direction):
            return _follow_sync_job_view(job)
    return {"status": "idle", "account_id": account_id,
            "direction": direction}


@router.post("/api/follow-sync-jobs/{job_id}/cancel")
async def cancel_follow_sync_job(job_id: str):
    job = app_runtime._follow_sync_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "同步任务不存在")
    if job.get("status") not in {"queued", "running"}:
        return _follow_sync_job_view(job)
    if job.get("phase") == "saving":
        raise HTTPException(409, "数据正在提交，不能中途取消")
    job["status"] = "canceling"
    job["updated_at"] = datetime.utcnow().isoformat()
    task = app_runtime._follow_sync_tasks.get(job_id)
    if task:
        task.cancel()
    return _follow_sync_job_view(job)
