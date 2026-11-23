"""自动评论规则配置与评论任务队列审核、编辑及执行接口。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import select

from app import runtime as app_runtime
from app.db import get_session
from app.engine.cadence import row_deadline
from app.models import CommentRule, CommentTask, DouyinAccount
from app.platforms.bilibili import resolve_bili_mid, resolve_bili_work
from app.platforms.douyin import resolve_aweme_id, resolve_sec_uid
from app.platforms.instagram import looks_like_ig_work, resolve_ig_user_target, resolve_ig_work_ref
from app.platforms.kuaishou import resolve_ks_photo_id, resolve_ks_user_id
from app.platforms.twitter import looks_like_twitter_tweet, resolve_twitter_short_url, resolve_twitter_tweet_ref, resolve_twitter_user_target
from app.platforms.xhs import resolve_note as xhs_resolve_note, resolve_user as xhs_resolve_user
from app.scheduling import utc_iso
from app.services import metadata as metadata_service
from app.submissions import replay_if_exists, submit_once

router = APIRouter()


# ─────────── 自动评论(规则 + 任务)───────────
class CommentRuleIn(BaseModel):
    platform: str = "douyin"
    name: str = ""
    mode: str = "auto_reply"            # auto_reply | auto_comment
    account_id: int
    target_kind: str = "self"          # reply: self|work ; comment: keyword|creator
    target: str = ""                   # 关键词,或 创作者/作品 的链接/id
    templates: list[str] = []
    use_ai: bool = False
    require_review: bool = False
    reply_filter: str = ""
    skip_keywords: str = ""
    daily_cap: int = 20
    min_gap_seconds: int = 90
    max_per_run: int = 5
    interval_seconds: int = 1800
    enabled: bool = False


class CommentRuleUpdate(BaseModel):
    name: str | None = None
    templates: list[str] | None = None
    use_ai: bool | None = None
    require_review: bool | None = None
    reply_filter: str | None = None
    skip_keywords: str | None = None
    daily_cap: int | None = None
    min_gap_seconds: int | None = None
    max_per_run: int | None = None
    interval_seconds: int | None = None
    enabled: bool | None = None
    # 改目标(任一非空则重新解析)。account_id 可单独改。
    account_id: int | None = None
    mode: str | None = None
    target_kind: str | None = None
    target: str | None = None


def _rule_dict(r: CommentRule) -> dict:
    return {
        "id": r.id, "platform": r.platform, "name": r.name, "mode": r.mode,
        "account_id": r.account_id, "target_kind": r.target_kind,
        "keyword": r.keyword, "sec_uid": r.sec_uid, "aweme_id": r.aweme_id,
        "templates": json.loads(r.templates or "[]"), "use_ai": r.use_ai,
        "require_review": r.require_review,
        "reply_filter": r.reply_filter, "skip_keywords": r.skip_keywords,
        "daily_cap": r.daily_cap, "min_gap_seconds": r.min_gap_seconds,
        "max_per_run": r.max_per_run, "interval_seconds": r.interval_seconds,
        "enabled": r.enabled, "last_error": r.last_error,
        "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
        "next_auto_run_at": utc_iso(row_deadline(r, app_runtime.cfg, kind="comment_rule")),
    }


async def _resolve_rule_target(
    platform: str, mode: str, target_kind: str, target: str, *, account_id: int | None = None
):
    """把 mode/target_kind/target 解析成 (kind, sec_uid, aweme_id, keyword, xsec_token)。
    解析失败抛 HTTPException。POST 与 PUT(改目标)共用。"""
    sec_uid = aweme_id = keyword = xsec_token = ""
    eff_proxy = metadata_service._resolve_ephemeral_proxy(platform, account_id)
    if mode == "auto_comment":
        kind = target_kind if target_kind in ("keyword", "creator") else "keyword"
        if kind == "keyword":
            keyword = (target or "").strip()
            if not keyword:
                raise HTTPException(400, "请填写要评论的搜索关键词")
            if platform in ("douyin", "kuaishou"):
                pn = "快手" if platform == "kuaishou" else "抖音"
                raise HTTPException(400, f"{pn}暂不支持关键词发现,请用「创作者」模式")
        else:
            if platform == "xhs":
                ref = await metadata_service._call_resolver(
                    xhs_resolve_user, target, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                )
                if not ref:
                    raise HTTPException(400, "无法解析小红书创作者(主页链接 / xhslink / user_id)")
                sec_uid, xsec_token = ref.user_id, ref.xsec_token
            elif platform == "kuaishou":
                sec_uid = await metadata_service._call_resolver(
                    resolve_ks_user_id, target, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                )
                if not sec_uid:
                    raise HTTPException(400, "无法解析快手创作者(主页链接 / 短链 / user_id)")
            elif platform == "bilibili":
                sec_uid = await metadata_service._call_resolver(
                    resolve_bili_mid, target, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                ) or ""
                if not sec_uid:
                    raise HTTPException(400, "无法解析 B 站 UP 主 UID(空间链接 / b23.tv / 数字 UID)")
            elif platform == "instagram":
                if looks_like_ig_work(target):
                    raise HTTPException(400, "创作者模式请填写 Instagram @username 或主页链接，不要填写单帖链接")
                sec_uid = resolve_ig_user_target(target)
                if not sec_uid:
                    raise HTTPException(400, "无法解析 Instagram 创作者(@username / 主页链接 / 数字 UID)")
            elif platform == "twitter":
                expanded_tw = await metadata_service._call_resolver(
                    resolve_twitter_short_url,
                    target,
                    user_agent=app_runtime.cfg.engine.user_agent,
                    proxy=eff_proxy,
                )
                if looks_like_twitter_tweet(expanded_tw):
                    raise HTTPException(400, "创作者模式请填写 Twitter @username 或主页链接，不要填写单条推文链接")
                sec_uid = resolve_twitter_user_target(expanded_tw)
                if not sec_uid:
                    raise HTTPException(400, "无法解析 Twitter 创作者(@username / x.com/<username> / 数字 UID)")
            else:
                sec_uid = await metadata_service._call_resolver(
                    resolve_sec_uid, target, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                )
                if not sec_uid:
                    raise HTTPException(400, "无法解析 sec_uid(主页链接 / 短链 / sec_uid)")
    else:
        kind = target_kind if target_kind in ("self", "work") else "self"
        if kind == "work":
            if platform == "xhs":
                ref = await metadata_service._call_resolver(
                    xhs_resolve_note, target, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                )
                if not ref:
                    raise HTTPException(400, "无法解析小红书笔记(explore 链接 / xhslink / note_id)")
                aweme_id, xsec_token = ref.note_id, ref.xsec_token
            elif platform == "kuaishou":
                aweme_id = await metadata_service._call_resolver(
                    resolve_ks_photo_id, target, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                )
                if not aweme_id:
                    raise HTTPException(400, "无法解析快手作品 id(作品链接 / 短链 / photo_id)")
            elif platform == "bilibili":
                aweme_id = await metadata_service._call_resolver(
                    resolve_bili_work, target, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                ) or ""
                if not aweme_id:
                    raise HTTPException(400, "无法解析 B 站稿件(BV 号 / 视频链接 / b23.tv)")
            elif platform == "instagram":
                wref = resolve_ig_work_ref(target)
                aweme_id = wref.shortcode if wref else ""
                if not aweme_id:
                    raise HTTPException(400, "无法解析 Instagram 作品(/p/<code>/ 或 /reel/<code>/ 链接或 shortcode)")
            elif platform == "twitter":
                expanded_tw = await metadata_service._call_resolver(
                    resolve_twitter_short_url,
                    target,
                    user_agent=app_runtime.cfg.engine.user_agent,
                    proxy=eff_proxy,
                )
                tref = resolve_twitter_tweet_ref(expanded_tw)
                aweme_id = tref.tweet_id if tref else ""
                if not aweme_id:
                    raise HTTPException(400, "无法解析 Twitter 推文(x.com/<user>/status/<id> 链接、t.co 短链或数字推文 ID)")
            else:
                aweme_id = await metadata_service._call_resolver(
                    resolve_aweme_id, target, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                )
                if not aweme_id:
                    raise HTTPException(400, "无法解析作品 id(作品链接 / 短链 / 数字 id)")
    return kind, sec_uid, aweme_id, keyword, xsec_token


@router.get("/api/comment-rules")
async def list_comment_rules(platform: str | None = None):
    with get_session() as s:
        q = select(CommentRule)
        if platform:
            q = q.where(CommentRule.platform == platform)
        return [_rule_dict(r) for r in s.exec(q.order_by(CommentRule.id.desc())).all()]


@router.post("/api/comment-rules")
async def add_comment_rule(body: CommentRuleIn, request: Request = None):
    if str(body.platform or "").strip().lower() in ("rednote", "tiktok"):
        raise HTTPException(400, "该平台暂未开放自动评论与回复计划")
    with get_session() as s:
        previous = replay_if_exists(s, request=request, scope="comment-rule", body=body)
        if previous is not None:
            return previous
    platform = body.platform if body.platform in ("douyin", "xhs", "kuaishou", "bilibili", "instagram", "twitter") else "douyin"
    mode = body.mode if body.mode in ("auto_reply", "auto_comment") else "auto_reply"
    templates = [t.strip() for t in body.templates if t.strip()]
    if not templates:
        raise HTTPException(400, "请至少配置一条文案模板(AI 生成失败时回退用)")
    _pn = {"xhs": "小红书", "kuaishou": "快手", "bilibili": "哔哩哔哩", "instagram": "Instagram", "twitter": "Twitter"}.get(platform, "抖音")
    with get_session() as s:
        acc = s.get(DouyinAccount, body.account_id)
        if not acc or acc.platform != platform:
            raise HTTPException(400, f"请选择一个已登录的{_pn}账号")
        if not (acc.storage_state or acc.creator_storage_state or acc.cookie):
            raise HTTPException(400, "该账号未登录,发评论需要登录态")
        if mode == "auto_comment" and str(acc.account_role or "hybrid").strip().lower() == "publish":
            raise HTTPException(
                400,
                "该账号角色设为「发布主号 (publish)」，禁止执行外部作品主动评论引流 (auto_comment) 以保护主号；仅允许自有作品评论自动回复 (auto_reply)",
            )

    kind, sec_uid, aweme_id, keyword, xsec_token = await _resolve_rule_target(
        platform, mode, body.target_kind, body.target, account_id=body.account_id)

    with get_session() as s:
        def create():
            r = CommentRule(
                platform=platform, name=body.name or ("自动回复" if mode == "auto_reply" else "自动评论"),
                mode=mode, account_id=body.account_id, target_kind=kind,
                keyword=keyword, sec_uid=sec_uid, aweme_id=aweme_id, xsec_token=xsec_token,
                templates=json.dumps(templates, ensure_ascii=False), use_ai=body.use_ai,
                require_review=body.require_review,
                reply_filter=body.reply_filter.strip(), skip_keywords=body.skip_keywords.strip(),
                daily_cap=max(0, body.daily_cap), min_gap_seconds=max(1, body.min_gap_seconds),
                max_per_run=max(1, body.max_per_run),
                interval_seconds=max(60, body.interval_seconds), enabled=body.enabled)
            s.add(r); s.flush()
            return _rule_dict(r)
        payload, _ = submit_once(s, request=request, scope="comment-rule", body=body, create=create)
        return payload


@router.put("/api/comment-rules/{rid}")
async def update_comment_rule(rid: int, body: CommentRuleUpdate):
    with get_session() as s:
        r = s.get(CommentRule, rid)
        if not r:
            raise HTTPException(404)
        platform = r.platform

    # 改账号:校验平台一致 + 已登录
    if body.account_id is not None:
        with get_session() as s:
            acc = s.get(DouyinAccount, body.account_id)
            if not acc or acc.platform != platform:
                raise HTTPException(400, "账号无效或与规则平台不一致")
            if not (acc.storage_state or acc.creator_storage_state):
                raise HTTPException(400, "该账号未登录,发评论需要登录态")

    # 改目标:mode/target_kind/target 任一传入则整体重解析
    new_target = None
    if body.mode is not None or body.target_kind is not None or body.target is not None:
        with get_session() as s:
            r = s.get(CommentRule, rid)
            mode = body.mode if body.mode in ("auto_reply", "auto_comment") else r.mode
            tk = body.target_kind if body.target_kind is not None else r.target_kind
            tgt = body.target if body.target is not None else ""
            eff_acc_id = body.account_id if body.account_id is not None else r.account_id
        new_target = (mode, *await _resolve_rule_target(platform, mode, tk, tgt, account_id=eff_acc_id))

    with get_session() as s:
        r = s.get(CommentRule, rid)
        if not r:
            raise HTTPException(404)
        if body.account_id is not None:
            r.account_id = body.account_id
        if new_target is not None:
            r.mode, r.target_kind, r.sec_uid, r.aweme_id, r.keyword, r.xsec_token = new_target
        if body.name is not None:
            r.name = body.name
        if body.templates is not None:
            tps = [t.strip() for t in body.templates if t.strip()]
            if not tps:
                raise HTTPException(400, "文案模板不能为空")
            r.templates = json.dumps(tps, ensure_ascii=False)
        if body.use_ai is not None:
            r.use_ai = body.use_ai
        if body.require_review is not None:
            r.require_review = body.require_review
        if body.reply_filter is not None:
            r.reply_filter = body.reply_filter.strip()
        if body.skip_keywords is not None:
            r.skip_keywords = body.skip_keywords.strip()
        if body.daily_cap is not None:
            r.daily_cap = max(0, body.daily_cap)
        if body.min_gap_seconds is not None:
            r.min_gap_seconds = max(1, body.min_gap_seconds)
        if body.max_per_run is not None:
            r.max_per_run = max(1, body.max_per_run)
        if body.interval_seconds is not None:
            r.interval_seconds = max(60, body.interval_seconds)
        if body.enabled is not None:
            r.enabled = body.enabled
        s.add(r); s.commit(); s.refresh(r)
        return _rule_dict(r)


@router.delete("/api/comment-rules/{rid}")
async def del_comment_rule(rid: int, with_tasks: bool = True):
    with get_session() as s:
        r = s.get(CommentRule, rid)
        if not r:
            return {"ok": True}
        if with_tasks:
            for t in s.exec(select(CommentTask).where(CommentTask.rule_id == rid)).all():
                s.delete(t)
        s.delete(r); s.commit()
    return {"ok": True}


@router.post("/api/comment-rules/{rid}/run-now")
async def run_comment_rule_now(rid: int):
    if not app_runtime.engine:
        raise HTTPException(503, "引擎未就绪")
    return await app_runtime.engine.run_comment_rule(rid)


@router.get("/api/comment-tasks")
async def list_comment_tasks(platform: str | None = None, rule_id: int | None = None,
                             status: str | None = None, limit: int = 200):
    with get_session() as s:
        q = select(CommentTask)
        if platform:
            q = q.where(CommentTask.platform == platform)
        if rule_id is not None:
            q = q.where(CommentTask.rule_id == rule_id)
        if status:
            q = q.where(CommentTask.status == status)
        rows = s.exec(q.order_by(CommentTask.id.desc()).limit(limit)).all()
        return [metadata_service._task_dict(t) for t in rows]


@router.post("/api/comment-tasks/{tid}/run-now")
async def run_comment_task_now(tid: int):
    if not app_runtime.engine:
        raise HTTPException(503, "引擎未就绪")
    with get_session() as s:
        t = s.get(CommentTask, tid)
        if not t:
            raise HTTPException(404)
        if t.status not in ("pending", "failed"):
            raise HTTPException(400, f"任务状态为 {t.status}")
        t.status = "pending"; t.scheduled_at = None; t.error = ""
        s.add(t); s.commit()
    return await app_runtime.engine.execute_comment_task(tid)


@router.post("/api/comment-tasks/{tid}/cancel")
async def cancel_comment_task(tid: int):
    with get_session() as s:
        t = s.get(CommentTask, tid)
        if not t:
            raise HTTPException(404)
        if t.status in ("draft", "pending", "failed"):
            t.status = "canceled"
            s.add(t); s.commit()
    return {"ok": True}


class IdsIn2(BaseModel):
    ids: list[int] = []


class TaskContentIn(BaseModel):
    content: str


@router.put("/api/comment-tasks/{tid}")
async def edit_comment_task(tid: int, body: TaskContentIn):
    """编辑草稿/待发任务的文案(草稿审核时人工微调用)。"""
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(400, "文案不能为空")
    with get_session() as s:
        t = s.get(CommentTask, tid)
        if not t:
            raise HTTPException(404)
        if t.status not in ("draft", "pending", "failed"):
            raise HTTPException(400, f"任务状态为 {t.status},不可编辑")
        t.content = content[:200]
        s.add(t); s.commit(); s.refresh(t)
        return metadata_service._task_dict(t)


def _approve_one(s, t) -> bool:
    """把 draft 任务转为 pending(通过审核)。返回是否改动。"""
    if t and t.status == "draft":
        # Approval changes review state, not the generated spacing/appointment.
        t.status = "pending"; t.error = ""
        s.add(t)
        return True
    return False


@router.post("/api/comment-tasks/{tid}/approve")
async def approve_comment_task(tid: int):
    """通过单条草稿:draft -> pending,引擎随后按节流自动发出。"""
    with get_session() as s:
        t = s.get(CommentTask, tid)
        if not t:
            raise HTTPException(404)
        if not _approve_one(s, t):
            raise HTTPException(400, f"任务状态为 {t.status},非草稿")
        s.commit()
    return {"ok": True}


class CommentBatchApproveIn(BaseModel):
    ids: list[int] = PydanticField(default_factory=list, max_length=500)
    platform: str
    account_id: int | None = None


@router.post("/api/comment-tasks/batch-approve")
async def batch_approve_comment_tasks(body: CommentBatchApproveIn):
    """Only approve an explicit, scope-checked selection, atomically."""
    if body.platform not in {"douyin", "xhs", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"}:
        raise HTTPException(422, "平台类型无效")
    ids = list(dict.fromkeys(body.ids))
    if not ids or any(value <= 0 for value in ids):
        raise HTTPException(422, "请选择明确的草稿 ID；空列表不会审核任何任务")
    n = 0
    with get_session() as s:
        rows = s.exec(select(CommentTask).where(CommentTask.id.in_(ids))).all()
        if len(rows) != len(ids) or any(
                t.status != "draft" or t.platform != body.platform
                or (body.account_id is not None and t.account_id != body.account_id)
                for t in rows):
            raise HTTPException(409, "所选草稿的状态或平台/账号范围已变化，本批次未审核，请刷新后重选")
        for t in rows:
            if _approve_one(s, t):
                n += 1
        s.commit()
    return {"ok": True, "approved": n}


@router.delete("/api/comment-tasks/{tid}")
async def del_comment_task(tid: int):
    with get_session() as s:
        t = s.get(CommentTask, tid)
        if t:
            if t.status in ("doing", "uncertain"):
                raise HTTPException(409, "任务正在执行或结果待核对，请先确认平台结果")
            s.delete(t); s.commit()
    return {"ok": True}


@router.post("/api/comment-tasks/batch-delete")
async def batch_del_comment_tasks(body: IdsIn2):
    n = 0
    with get_session() as s:
        for tid in body.ids:
            t = s.get(CommentTask, tid)
            if t:
                if t.status in ("doing", "uncertain"):
                    raise HTTPException(409, f"任务 #{tid} 正在执行或结果待核对，本批次未删除")
                s.delete(t); n += 1
        s.commit()
    return {"ok": True, "deleted": n}
