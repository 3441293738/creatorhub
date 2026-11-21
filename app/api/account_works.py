"""本账号作品同步、作品级评论/弹幕同步、统计快照及海外小红书作品管理接口。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from app import runtime as app_runtime
from app.browser import fetch_account_works, fetch_douyin_account_works_api
from app.db import get_session
from app.models import AccountStatSnapshot, AccountWork, CommentRecord, DanmakuRecord, DouyinAccount
from app.platforms.bilibili import BilibiliClient
from app.platforms.douyin import cookie_from_state as douyin_cookie_from_state
from app.platforms.instagram import InstagramClient
from app.platforms.rednote import delete_rednote_work, update_rednote_visibility
from app.risk import OperationKind
from app.services import watch_records as watch_records_service
from app.transport_matrix import douyin_client_environment, resolve_transport

router = APIRouter()


# ─────────── 本账号管理:作品 ───────────
def _work_dict(w: AccountWork) -> dict:
    return {
        "id": w.id, "platform": w.platform, "account_id": w.account_id,
        "item_id": w.item_id, "desc": w.desc, "media_type": w.media_type,
        "cover_url": w.cover_url, "create_time": w.create_time,
        "like_count": w.like_count, "comment_count": w.comment_count,
        "collect_count": w.collect_count, "share_count": w.share_count,
        "play_count": w.play_count, "status": w.status,
        "fetched_at": w.fetched_at.isoformat() if w.fetched_at else None,
    }


@router.get("/api/account-works")
async def list_account_works(account_id: int, limit: int = 200):
    with get_session() as s:
        q = (select(AccountWork).where(AccountWork.account_id == account_id)
             .order_by(AccountWork.create_time.desc()).limit(limit))
        return [_work_dict(w) for w in s.exec(q).all()]


@router.post("/api/accounts/{account_id}/works/sync")
async def sync_account_works(account_id: int):
    """打开账号自己的主页,拦截抓取本账号已发布作品,落库(upsert)。"""
    if app_runtime.engine is None:
        raise HTTPException(503, "引擎未就绪")
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        if acc.status == "invalid":
            raise HTTPException(400, "账号登录态已失效")
        if app_runtime.engine._proxy_bad(acc):
            raise HTTPException(400, "账号代理不可用")
        platform = acc.platform
        uid = (
            acc.douyin_id if platform == "tiktok"
            else (acc.douyin_id or acc.sec_uid or acc.uid) if platform == "twitter"
            else (acc.sec_uid or acc.uid or acc.douyin_id) if platform == "instagram"
            else acc.sec_uid
        ) or ""
        transport = (
            resolve_transport(app_runtime.cfg, platform, "account_works", acc)
            if platform in {"douyin", "bilibili", "rednote", "instagram", "twitter"}
            else None
        )
        effective_mode = transport["effective_mode"] if transport else "browser"
        if app_runtime.browser is None and effective_mode in {"browser"}:
            raise HTTPException(503, "浏览器未就绪")
        if app_runtime.browser is None and effective_mode == "hybrid" and platform not in {"bilibili", "rednote", "instagram"}:
            raise HTTPException(503, "浏览器未就绪")
        identity = (
            None
            if platform in {"douyin", "bilibili", "rednote", "instagram"} and effective_mode == "api"
            else (
                app_runtime.browser.identity_for(acc)
                if callable(getattr(app_runtime.browser, "identity_for", None))
                else None
            )
        )
        direct_ua = (identity.ua if identity is not None
                     else (acc.ua or app_runtime.cfg.engine.user_agent))
        direct_environment = douyin_client_environment(
            identity if identity is not None else acc)
        source = "api" if effective_mode == "api" else "browser"
        bili_state = (acc.storage_state or acc.cookie or "") if platform == "bilibili" else ""
        bili_proxy = (acc.proxy or "") if platform == "bilibili" else ""
        ig_state = (acc.storage_state or acc.creator_storage_state or acc.cookie or "") if platform == "instagram" else ""
        ig_proxy = (acc.proxy or "") if platform == "instagram" else ""
        ig_seed = (acc.fp_seed or acc.profile_dir or "") if platform == "instagram" else ""
        ig_tz = (acc.timezone_id or "Asia/Shanghai") if platform == "instagram" else "Asia/Shanghai"
    async def _fetch():
        nonlocal source
        if platform == "douyin" and effective_mode in {"api", "hybrid"}:
            cookie = douyin_cookie_from_state(acc.storage_state or acc.creator_storage_state or "")
            api_items, api_err = await fetch_douyin_account_works_api(
                cookie, direct_ua, uid,
                timeout=app_runtime.cfg.engine.request_timeout_seconds, proxy=acc.proxy or "",
                environment=direct_environment)
            if api_items or effective_mode == "api" or api_err == "empty":
                source = "api"
                return api_items, api_err
            print(f"[dy-account-works] API 空响应({api_err}),回退浏览器")
            source = "browser_fallback"
        elif platform == "bilibili" and effective_mode in {"api", "hybrid"} and bili_state:
            bcli = BilibiliClient(
                bili_state,
                direct_ua,
                proxy=bili_proxy,
                timeout=app_runtime.cfg.engine.request_timeout_seconds,
            )
            async with bcli.session_scope():
                b_works, b_err = await bcli.fetch_all_creator_archives(max_pages=6)
            if b_works or b_err == "":
                source = "api"
                return b_works, ("" if b_works else "empty_confirmed")
            if effective_mode == "api" or b_err.startswith("logged_out"):
                source = "api"
                return [], b_err or "empty"
            source = "browser_fallback"
        elif platform == "instagram" and effective_mode in {"api", "hybrid"} and ig_state:
            icli = InstagramClient(
                ig_state,
                direct_ua,
                proxy=ig_proxy,
                timeout=app_runtime.cfg.engine.request_timeout_seconds,
                account_seed=ig_seed,
                timezone_id=ig_tz,
            )
            async with icli.session_scope():
                target_u = uid or icli.self_uid
                if not target_u:
                    prof, perr = await icli.fetch_self_profile()
                    if perr:
                        return [], perr
                    target_u = str(prof.get("uid") or prof.get("unique_id") or "")
                ig_works, ig_err = await icli.fetch_all_creator_works(target_u, max_pages=6)
            if ig_works or ig_err == "":
                source = "api"
                return ig_works, ("" if ig_works else "empty_confirmed")
            if effective_mode == "api" or ig_err.startswith(("logged_out", "verification_required")) or app_runtime.browser is None:
                source = "api"
                return [], ig_err or "empty"
            source = "browser_fallback"
        else:
            source = "browser"
        return await fetch_account_works(app_runtime.browser, identity, platform, uid)

    items, err = await app_runtime.engine.guarded_read_pair(
        account_id, OperationKind.READ_LIGHT, f"account-works:{account_id}",
        _fetch, empty_result=[])
    if err.startswith("risk_deferred:"):
        return {"ok": True, "fetched": 0, "added": 0, "skipped": True,
                "reason": err.split(":", 1)[-1], "source": "deferred",
                "configured_mode": (transport["configured_mode"]
                                    if transport else "browser")}
    if not items and err == "empty_confirmed":
        return {"ok": True, "fetched": 0, "added": 0, "empty": True,
                "source": source,
                "configured_mode": (transport["configured_mode"]
                                    if transport else "browser")}
    if not items:
        if err and err.startswith("missing_uid"):
            raise HTTPException(400, err.split(":", 1)[-1])
        raise HTTPException(400, f"未抓到作品:{err or '可能登录态失效/无公开作品'}"
                                 "(详情见服务端控制台日志)")
    now = datetime.utcnow()
    added = 0
    with get_session() as s:
        for w in items:
            existing = s.exec(select(AccountWork).where(
                AccountWork.account_id == account_id,
                AccountWork.item_id == w["item_id"])).first()
            if existing:
                for k, v in w.items():
                    setattr(existing, k, v)
                existing.fetched_at = now
                s.add(existing)
            else:
                s.add(AccountWork(platform=platform, account_id=account_id,
                                  fetched_at=now, **w))
                added += 1
        s.commit()
    return {"ok": True, "fetched": len(items), "added": added,
            "source": source,
            "configured_mode": (transport["configured_mode"]
                                if transport else "browser")}


# ─────────── 本账号数据分析(B4:粉丝/作品/互动趋势 + 单篇作品表)───────────
@router.get("/api/account-stats/{account_id}")
async def account_stats(account_id: int, days: int = 30):
    """返回该账号近 days 天的每日快照趋势 + 当前本账号作品的单篇互动明细。
    快照由引擎在账号体检/作品健康时写入(见 EngineConfig.work_health_*)。"""
    days = max(1, min(days, 180))
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        snaps = s.exec(select(AccountStatSnapshot)
                       .where(AccountStatSnapshot.account_id == account_id)
                       .order_by(AccountStatSnapshot.date.desc()).limit(days)).all()
        works = s.exec(select(AccountWork)
                       .where(AccountWork.account_id == account_id)
                       .order_by(AccountWork.create_time.desc()).limit(50)).all()
    trend = [{"date": x.date, "follower_count": x.follower_count,
              "aweme_count": x.aweme_count, "total_like": x.total_like,
              "total_comment": x.total_comment, "total_play": x.total_play}
             for x in reversed(snaps)]
    latest = trend[-1] if trend else {}
    prev = trend[-2] if len(trend) >= 2 else {}
    fans_delta = (latest.get("follower_count", 0) - prev.get("follower_count", 0)
                  if prev else 0)
    return {
        "account": {"id": acc.id, "platform": acc.platform, "nickname": acc.nickname,
                    "follower_count": acc.follower_count, "aweme_count": acc.aweme_count},
        "fans_delta": fans_delta,
        "trend": trend,
        "works": [_work_dict(w) for w in works],
    }


# ─────────── 本账号管理:作品评论(抖音直连分页 / 小红书客户端 / 快手拦截)───────────
@router.get("/api/account-works/{work_id}/comments")
async def list_work_comments(work_id: int, limit: int = 300):
    with get_session() as s:
        w = s.get(AccountWork, work_id)
        if not w:
            raise HTTPException(404, "作品不存在")
        item_id = w.item_id
        rows = s.exec(select(CommentRecord).where(
            CommentRecord.watch_id == 0,
            CommentRecord.aweme_id == item_id)
            .order_by(CommentRecord.id.desc()).limit(limit)).all()
        return [watch_records_service._comment_dict(c) for c in rows]


@router.post("/api/account-works/{work_id}/comments/sync")
async def sync_work_comments(work_id: int):
    if app_runtime.engine is None:
        raise HTTPException(503, "引擎未就绪")
    with get_session() as s:
        w = s.get(AccountWork, work_id)
        if not w:
            raise HTTPException(404, "作品不存在")
        platform, item_id = w.platform, w.item_id
        account_id, xsec_token = w.account_id, w.xsec_token
    res = await app_runtime.engine.sync_work_comments(account_id, platform, item_id, xsec_token)
    if not res.get("ok") and not res.get("added"):
        raise HTTPException(400, f"抓评论失败:{res.get('error') or '未知'}"
                                 "(详情见服务端控制台日志)")
    return res


# ─────────── 本账号管理:作品弹幕(抖音创作者中心)───────────
@router.get("/api/account-works/{work_id}/danmaku")
async def list_work_danmaku(work_id: int, limit: int = 300):
    with get_session() as s:
        w = s.get(AccountWork, work_id)
        if not w:
            raise HTTPException(404, "作品不存在")
        rows = s.exec(select(DanmakuRecord).where(
            DanmakuRecord.watch_id == 0,
            DanmakuRecord.aweme_id == w.item_id)
            .order_by(DanmakuRecord.id.desc()).limit(limit)).all()
        return [watch_records_service._danmaku_dict(row) for row in rows]


@router.post("/api/account-works/{work_id}/danmaku/sync")
async def sync_work_danmaku(work_id: int):
    if app_runtime.engine is None:
        raise HTTPException(503, "引擎未就绪")
    with get_session() as s:
        w = s.get(AccountWork, work_id)
        if not w:
            raise HTTPException(404, "作品不存在")
        platform, item_id, account_id = w.platform, w.item_id, w.account_id
    res = await app_runtime.engine.sync_work_danmaku(account_id, platform, item_id)
    if not res.get("ok") and not res.get("added"):
        raise HTTPException(400, f"抓弹幕失败:{res.get('error') or '未知'}"
                                 "(详情见服务端控制台日志)")
    return res


class RednoteVisibilityIn(BaseModel):
    visibility: Literal["public", "private"] = "public"


@router.post("/api/account-works/{work_id}/rednote-visibility")
async def set_rednote_work_visibility(work_id: int, body: RednoteVisibilityIn):
    """修改海外小红书已发布作品的可见范围（public / private）。"""
    with get_session() as s:
        work = s.get(AccountWork, work_id)
        if not work:
            raise HTTPException(404, "作品不存在")
        if work.platform != "rednote":
            raise HTTPException(400, "该接口仅支持海外小红书作品")
        acc = s.get(DouyinAccount, work.account_id)
        if not acc or acc.platform != "rednote":
            raise HTTPException(400, "绑定账号不存在或非海外小红书账号")
        state = acc.creator_storage_state or acc.storage_state or ""
        proxy = acc.proxy or ""
        ua = acc.ua or app_runtime.cfg.engine.user_agent
        item_id = work.item_id
    if not state:
        raise HTTPException(400, "该海外小红书账号尚未登录")
    ok, err = await update_rednote_visibility(
        state,
        item_id,
        body.visibility,
        proxy=proxy,
        user_agent=ua,
    )
    if not ok:
        raise HTTPException(400, err or "修改海外小红书作品可见性失败")
    with get_session() as s:
        work = s.get(AccountWork, work_id)
        if work:
            work.status = "仅自己可见" if body.visibility == "private" else ""
            s.add(work)
            s.commit()
    return {"ok": True, "work_id": work_id, "item_id": item_id, "visibility": body.visibility}


@router.api_route("/api/account-works/{work_id}/rednote-delete", methods=["POST", "DELETE"])
async def remove_rednote_work(work_id: int):
    """删除海外小红书已发布作品（调用 webapi.rednote.com/web_api/sns/capa/postgw/note/delete）。"""
    with get_session() as s:
        work = s.get(AccountWork, work_id)
        if not work:
            raise HTTPException(404, "作品不存在")
        if work.platform != "rednote":
            raise HTTPException(400, "该接口仅支持海外小红书作品")
        acc = s.get(DouyinAccount, work.account_id)
        if not acc or acc.platform != "rednote":
            raise HTTPException(400, "绑定账号不存在或非海外小红书账号")
        state = acc.creator_storage_state or acc.storage_state or ""
        proxy = acc.proxy or ""
        ua = acc.ua or app_runtime.cfg.engine.user_agent
        item_id = work.item_id
    if not state:
        raise HTTPException(400, "该海外小红书账号尚未登录")
    ok, err = await delete_rednote_work(
        state, item_id, proxy=proxy, user_agent=ua
    )
    if not ok:
        raise HTTPException(400, err or "删除海外小红书作品失败")
    with get_session() as s:
        work = s.get(AccountWork, work_id)
        if work:
            s.delete(work)
            s.commit()
    return {"ok": True, "work_id": work_id, "item_id": item_id}
