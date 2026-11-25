"""自动评论规则配置、试跑预览与评论任务队列审核、重写、编辑及执行接口。"""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import select

from app import runtime as app_runtime
from app.ai_copywriting import all_presets, get_copywriting_config, get_shared_channels_summary
from app.db import get_session
from app.engine.cadence import row_deadline
from app.engine.comment_rules_runner import (
    VALID_COMMENT_SCOPES,
    VALID_COMPOSE_MODES,
    VALID_MATCH_MODES,
    effective_compose_mode,
    parse_keyword_list,
)
from app.models import CommentRule, CommentTask, DouyinAccount
from app.platforms.bilibili import resolve_bili_mid, resolve_bili_work
from app.platforms.douyin import resolve_aweme_id, resolve_sec_uid
from app.platforms.facebook import looks_like_facebook_post, resolve_facebook_post_ref, resolve_facebook_short_url, resolve_facebook_user_target
from app.platforms.instagram import looks_like_ig_work, resolve_ig_user_target, resolve_ig_work_ref
from app.platforms.kuaishou import resolve_ks_photo_id, resolve_ks_user_id
from app.platforms.twitter import looks_like_twitter_tweet, resolve_twitter_short_url, resolve_twitter_tweet_ref, resolve_twitter_user_target
from app.platforms.youtube import looks_like_youtube_video, resolve_youtube_channel_target, resolve_youtube_video_ref
from app.platforms.xhs import resolve_note as xhs_resolve_note, resolve_user as xhs_resolve_user
from app.scheduling import utc_iso
from app.services import metadata as metadata_service
from app.submissions import replay_if_exists, submit_once

router = APIRouter()


def _validate_regex_patterns(patterns: list[str]) -> None:
    for pat in patterns:
        try:
            re.compile(pat, re.IGNORECASE)
        except re.error as exc:
            raise HTTPException(400, f"包含关键词正则表达式无效（{pat}）：{exc}") from exc


def _normalize_compose_mode(compose_mode: str | None, use_ai: bool | None, *, fallback: str = "template_only") -> tuple[str, bool]:
    cm = str(compose_mode or "").strip().lower()
    if cm in VALID_COMPOSE_MODES:
        return cm, cm != "template_only"
    if use_ai is not None:
        cm = "ai_with_fallback" if use_ai else "template_only"
        return cm, bool(use_ai)
    return fallback, fallback != "template_only"


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
    compose_mode: str = ""             # template_only | ai_with_fallback | ai_only_draft | ai_only_skip
    ai_channel_id: str = ""
    ai_model: str = ""
    ai_preset_id: str = ""
    ai_prompt: str = ""
    ai_max_chars: int = 0
    require_review: bool = False
    match_mode: str = "any"            # all | any | all_terms | regex
    include_keywords: list[str] | None = None
    exclude_keywords: list[str] | None = None
    reply_filter: str = ""
    skip_keywords: str = ""
    comment_scope: str = "all"         # all | top_only | reply_only
    min_source_length: int = 0
    max_source_age_hours: int = 0
    recent_works: int = 0
    recent_days: int = 0
    daily_cap: int = 20
    min_gap_seconds: int = 90
    max_per_run: int = 5
    interval_seconds: int = 1800
    enabled: bool = False


class CommentRuleUpdate(BaseModel):
    name: str | None = None
    templates: list[str] | None = None
    use_ai: bool | None = None
    compose_mode: str | None = None
    ai_channel_id: str | None = None
    ai_model: str | None = None
    ai_preset_id: str | None = None
    ai_prompt: str | None = None
    ai_max_chars: int | None = None
    require_review: bool | None = None
    match_mode: str | None = None
    include_keywords: list[str] | None = None
    exclude_keywords: list[str] | None = None
    reply_filter: str | None = None
    skip_keywords: str | None = None
    comment_scope: str | None = None
    min_source_length: int | None = None
    max_source_age_hours: int | None = None
    recent_works: int | None = None
    recent_days: int | None = None
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
    cm = effective_compose_mode(r)
    inc = parse_keyword_list(getattr(r, "include_keywords", "[]"), fallback_text=getattr(r, "reply_filter", ""))
    exc = parse_keyword_list(getattr(r, "exclude_keywords", "[]"), fallback_text=getattr(r, "skip_keywords", ""))
    mm = str(getattr(r, "match_mode", "any") or "any").strip().lower()
    if mm not in VALID_MATCH_MODES:
        mm = "any"
    scope = str(getattr(r, "comment_scope", "all") or "all").strip().lower()
    if scope not in VALID_COMMENT_SCOPES:
        scope = "all"
    return {
        "id": r.id, "platform": r.platform, "name": r.name, "mode": r.mode,
        "account_id": r.account_id, "target_kind": r.target_kind,
        "keyword": r.keyword, "sec_uid": r.sec_uid, "aweme_id": r.aweme_id,
        "templates": json.loads(r.templates or "[]"),
        "use_ai": cm != "template_only",
        "compose_mode": cm,
        "ai_channel_id": getattr(r, "ai_channel_id", "") or "",
        "ai_model": getattr(r, "ai_model", "") or "",
        "ai_preset_id": getattr(r, "ai_preset_id", "") or "",
        "ai_prompt": getattr(r, "ai_prompt", "") or "",
        "ai_max_chars": int(getattr(r, "ai_max_chars", 0) or 0),
        "require_review": r.require_review,
        "match_mode": mm,
        "include_keywords": inc,
        "exclude_keywords": exc,
        "reply_filter": r.reply_filter or (", ".join(inc) if inc else ""),
        "skip_keywords": r.skip_keywords or (", ".join(exc) if exc else ""),
        "comment_scope": scope,
        "min_source_length": int(getattr(r, "min_source_length", 0) or 0),
        "max_source_age_hours": int(getattr(r, "max_source_age_hours", 0) or 0),
        "recent_works": int(getattr(r, "recent_works", 0) or 0),
        "recent_days": int(getattr(r, "recent_days", 0) or 0),
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
            elif platform == "facebook":
                expanded_fb = await metadata_service._call_resolver(
                    resolve_facebook_short_url,
                    target,
                    user_agent=app_runtime.cfg.engine.user_agent,
                    proxy=eff_proxy,
                )
                if looks_like_facebook_post(expanded_fb):
                    raise HTTPException(400, "创作者模式请填写 Facebook 个人主页、公共主页或群组链接，不要填写单条帖子链接")
                sec_uid = resolve_facebook_user_target(expanded_fb)
                if not sec_uid:
                    raise HTTPException(400, "无法解析 Facebook 目标(主页/公共主页/群组链接、用户名或数字 ID)")
            elif platform == "youtube":
                if looks_like_youtube_video(target):
                    raise HTTPException(400, "创作者模式请填写 YouTube 频道主页链接（/@handle 或 /channel/UC...），不要填写单条视频链接")
                sec_uid, _yt_ch_url = resolve_youtube_channel_target(target)
                if not sec_uid:
                    raise HTTPException(400, "无法解析 YouTube 频道目标（支持 youtube.com/@handle、/channel/UC... 或 @handle）")
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
            elif platform == "facebook":
                expanded_fb = await metadata_service._call_resolver(
                    resolve_facebook_short_url,
                    target,
                    user_agent=app_runtime.cfg.engine.user_agent,
                    proxy=eff_proxy,
                )
                fref = resolve_facebook_post_ref(expanded_fb)
                aweme_id = fref.canonical_url or fref.post_id if fref else ""
                if not aweme_id:
                    raise HTTPException(400, "无法解析 Facebook 帖子(帖子/Reel/Watch 链接、fb.watch 短链或帖子 ID)")
            elif platform == "youtube":
                yref = resolve_youtube_video_ref(target)
                aweme_id = yref.video_id if yref else ""
                if not aweme_id:
                    raise HTTPException(400, "无法解析 YouTube 视频（支持 youtube.com/watch?v=...、youtu.be/...、/shorts/... 或 11 位视频 ID）")
            else:
                aweme_id = await metadata_service._call_resolver(
                    resolve_aweme_id, target, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                )
                if not aweme_id:
                    raise HTTPException(400, "无法解析作品 id(作品链接 / 短链 / 数字 id)")
    return kind, sec_uid, aweme_id, keyword, xsec_token


@router.get("/api/comment-rules/effective-policy")
async def get_comment_effective_policy(
    platform: str = "douyin",
    rule_daily_cap: int = 0,
    rule_min_gap: int = 0,
    rule_require_review: bool = False,
    rule_channel_id: str = "",
    rule_model: str = "",
):
    """返回当前平台的生效限额/风控间隔/审核覆盖/AI渠道与人设预设列表，供规则页实时提示。"""
    if app_runtime.engine is None:
        raise HTTPException(503, "引擎未就绪")
    policy = app_runtime.engine.effective_comment_policy(
        platform=platform,
        rule_daily_cap=rule_daily_cap,
        rule_min_gap=rule_min_gap,
        rule_require_review=rule_require_review,
        rule_channel_id=rule_channel_id,
        rule_model=rule_model,
    )
    ai_cfg = get_copywriting_config()
    return {
        "policy": policy,
        "ai_channels": get_shared_channels_summary(),
        "ai_presets": [p.model_dump() for p in all_presets(ai_cfg)],
        "ai_default_preset_id": ai_cfg.default_preset_id,
    }


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
    platform = body.platform if body.platform in ("douyin", "xhs", "kuaishou", "bilibili", "instagram", "twitter", "facebook", "youtube") else "douyin"
    mode = body.mode if body.mode in ("auto_reply", "auto_comment") else "auto_reply"
    compose_mode, use_ai = _normalize_compose_mode(body.compose_mode, body.use_ai)
    templates = [t.strip() for t in body.templates if t.strip()]
    if compose_mode in ("template_only", "ai_with_fallback") and not templates:
        raise HTTPException(400, "当前文案模式需至少配置一条文案模板（纯 AI 模式可在上方切换为「仅 AI」）")

    match_mode = str(body.match_mode or "any").strip().lower()
    if match_mode not in VALID_MATCH_MODES:
        match_mode = "any"
    include_kw = parse_keyword_list(
        body.include_keywords if body.include_keywords is not None else [],
        fallback_text=body.reply_filter,
    )
    exclude_kw = parse_keyword_list(
        body.exclude_keywords if body.exclude_keywords is not None else [],
        fallback_text=body.skip_keywords,
    )
    if match_mode == "regex" and include_kw:
        _validate_regex_patterns(include_kw)
    comment_scope = str(body.comment_scope or "all").strip().lower()
    if comment_scope not in VALID_COMMENT_SCOPES:
        comment_scope = "all"

    _pn = {"xhs": "小红书", "kuaishou": "快手", "bilibili": "哔哩哔哩", "instagram": "Instagram", "twitter": "Twitter", "facebook": "Facebook", "youtube": "YouTube"}.get(platform, "抖音")
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
                platform=platform,
                name=(body.name or "").strip()[:100] or ("自动回复" if mode == "auto_reply" else "自动评论"),
                mode=mode, account_id=body.account_id, target_kind=kind,
                keyword=keyword, sec_uid=sec_uid, aweme_id=aweme_id, xsec_token=xsec_token,
                templates=json.dumps(templates, ensure_ascii=False),
                use_ai=use_ai,
                compose_mode=compose_mode,
                ai_channel_id=(body.ai_channel_id or "").strip()[:64],
                ai_model=(body.ai_model or "").strip()[:160],
                ai_preset_id=(body.ai_preset_id or "").strip()[:64],
                ai_prompt=(body.ai_prompt or "").strip()[:2000],
                ai_max_chars=max(0, min(300, int(body.ai_max_chars or 0))),
                require_review=body.require_review,
                match_mode=match_mode,
                include_keywords=json.dumps(include_kw, ensure_ascii=False),
                exclude_keywords=json.dumps(exclude_kw, ensure_ascii=False),
                reply_filter=", ".join(include_kw),
                skip_keywords=", ".join(exclude_kw),
                comment_scope=comment_scope,
                min_source_length=max(0, min(500, int(body.min_source_length or 0))),
                max_source_age_hours=max(0, min(8760, int(body.max_source_age_hours or 0))),
                recent_works=max(0, min(50, int(body.recent_works or 0))),
                recent_days=max(0, min(365, int(body.recent_days or 0))),
                daily_cap=max(0, body.daily_cap),
                min_gap_seconds=max(1, body.min_gap_seconds),
                max_per_run=max(1, min(100, body.max_per_run)),
                interval_seconds=max(60, body.interval_seconds),
                enabled=body.enabled)
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
        cur_mode = r.mode
        cur_account_id = r.account_id

    eff_mode = body.mode if body.mode in ("auto_reply", "auto_comment") else cur_mode
    eff_acc_id = body.account_id if body.account_id is not None else cur_account_id

    # 校验账号：平台一致 + 已登录（含 cookie）+ publish 主号不可绑定 auto_comment
    if body.account_id is not None or body.mode is not None:
        with get_session() as s:
            acc = s.get(DouyinAccount, eff_acc_id) if eff_acc_id else None
            if not acc or acc.platform != platform:
                raise HTTPException(400, "账号无效或与规则平台不一致")
            if not (acc.storage_state or acc.creator_storage_state or acc.cookie):
                raise HTTPException(400, "该账号未登录,发评论需要登录态")
            if eff_mode == "auto_comment" and str(acc.account_role or "hybrid").strip().lower() == "publish":
                raise HTTPException(
                    400,
                    "该账号角色设为「发布主号 (publish)」，禁止执行外部作品主动评论引流 (auto_comment) 以保护主号",
                )

    # 改目标:仅在 mode / target_kind / target 确实变化时才重新解析，避免误清空小红书 xsec_token
    new_target = None
    if body.mode is not None or body.target_kind is not None or body.target is not None:
        with get_session() as s:
            r = s.get(CommentRule, rid)
            tk = body.target_kind if body.target_kind is not None else r.target_kind
            cur_target_val = (
                (r.keyword if r.target_kind == "keyword" else r.sec_uid)
                if r.mode == "auto_comment"
                else (r.aweme_id if r.target_kind == "work" else "")
            )
            tgt = body.target.strip() if body.target is not None else cur_target_val
            target_changed = (eff_mode != r.mode) or (tk != r.target_kind) or (tgt != (cur_target_val or "").strip())
        if target_changed:
            new_target = (eff_mode, *await _resolve_rule_target(platform, eff_mode, tk, tgt, account_id=eff_acc_id))

    with get_session() as s:
        r = s.get(CommentRule, rid)
        if not r:
            raise HTTPException(404)
        if body.account_id is not None:
            r.account_id = body.account_id
        if new_target is not None:
            r.mode, r.target_kind, r.sec_uid, r.aweme_id, r.keyword, r.xsec_token = new_target
        if body.name is not None:
            r.name = body.name.strip()[:100] or r.name

        cur_cm = effective_compose_mode(r)
        if body.compose_mode is not None or body.use_ai is not None:
            next_cm, next_use_ai = _normalize_compose_mode(body.compose_mode, body.use_ai, fallback=cur_cm)
        else:
            next_cm, next_use_ai = cur_cm, (cur_cm != "template_only")

        next_templates = (
            [t.strip() for t in body.templates if t.strip()]
            if body.templates is not None
            else json.loads(r.templates or "[]")
        )
        if next_cm in ("template_only", "ai_with_fallback") and not next_templates:
            raise HTTPException(400, "当前文案模式需要至少保留一条本地模板")

        r.compose_mode = next_cm
        r.use_ai = next_use_ai
        if body.templates is not None:
            r.templates = json.dumps(next_templates, ensure_ascii=False)
        if body.ai_channel_id is not None:
            r.ai_channel_id = body.ai_channel_id.strip()[:64]
        if body.ai_model is not None:
            r.ai_model = body.ai_model.strip()[:160]
        if body.ai_preset_id is not None:
            r.ai_preset_id = body.ai_preset_id.strip()[:64]
        if body.ai_prompt is not None:
            r.ai_prompt = body.ai_prompt.strip()[:2000]
        if body.ai_max_chars is not None:
            r.ai_max_chars = max(0, min(300, int(body.ai_max_chars or 0)))

        if body.require_review is not None:
            r.require_review = body.require_review

        next_mm = str(getattr(r, "match_mode", "any") or "any").strip().lower()
        if body.match_mode is not None:
            cand_mm = body.match_mode.strip().lower()
            if cand_mm in VALID_MATCH_MODES:
                next_mm = cand_mm
            r.match_mode = next_mm

        if body.include_keywords is not None or body.reply_filter is not None:
            inc = parse_keyword_list(
                body.include_keywords if body.include_keywords is not None else [],
                fallback_text=body.reply_filter or "",
            )
            if next_mm == "regex" and inc:
                _validate_regex_patterns(inc)
            r.include_keywords = json.dumps(inc, ensure_ascii=False)
            r.reply_filter = ", ".join(inc)
        elif body.match_mode == "regex":
            existing_inc = parse_keyword_list(getattr(r, "include_keywords", "[]"), fallback_text=r.reply_filter)
            if existing_inc:
                _validate_regex_patterns(existing_inc)

        if body.exclude_keywords is not None or body.skip_keywords is not None:
            exc = parse_keyword_list(
                body.exclude_keywords if body.exclude_keywords is not None else [],
                fallback_text=body.skip_keywords or "",
            )
            r.exclude_keywords = json.dumps(exc, ensure_ascii=False)
            r.skip_keywords = ", ".join(exc)

        if body.comment_scope is not None:
            sc = body.comment_scope.strip().lower()
            if sc in VALID_COMMENT_SCOPES:
                r.comment_scope = sc
        if body.min_source_length is not None:
            r.min_source_length = max(0, min(500, int(body.min_source_length or 0)))
        if body.max_source_age_hours is not None:
            r.max_source_age_hours = max(0, min(8760, int(body.max_source_age_hours or 0)))
        if body.recent_works is not None:
            r.recent_works = max(0, min(50, int(body.recent_works or 0)))
        if body.recent_days is not None:
            r.recent_days = max(0, min(365, int(body.recent_days or 0)))

        if body.daily_cap is not None:
            r.daily_cap = max(0, body.daily_cap)
        if body.min_gap_seconds is not None:
            r.min_gap_seconds = max(1, body.min_gap_seconds)
        if body.max_per_run is not None:
            r.max_per_run = max(1, min(100, body.max_per_run))
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


class DuplicateRuleIn(BaseModel):
    account_id: int | None = None
    name: str = ""


@router.post("/api/comment-rules/{rid}/duplicate")
async def duplicate_comment_rule(rid: int, body: DuplicateRuleIn | None = None):
    """一键复制规则（可绑定到同平台其他账号，默认关闭以便确认后启用）。"""
    body = body or DuplicateRuleIn()
    with get_session() as s:
        src = s.get(CommentRule, rid)
        if not src:
            raise HTTPException(404, "源规则不存在")
        target_acc_id = body.account_id if body.account_id is not None else src.account_id
        acc = s.get(DouyinAccount, target_acc_id) if target_acc_id else None
        if not acc or acc.platform != src.platform:
            raise HTTPException(400, "目标账号不存在或与源规则平台不一致")
        if not (acc.storage_state or acc.creator_storage_state or acc.cookie):
            raise HTTPException(400, "目标账号未登录，发评论需要登录态")
        if src.mode == "auto_comment" and str(acc.account_role or "hybrid").strip().lower() == "publish":
            raise HTTPException(400, "目标账号角色为「发布主号 (publish)」，禁止复制主动评论规则")

        suffix = f"（{acc.nickname}）" if (body.account_id and body.account_id != src.account_id and acc.nickname) else "（副本）"
        new_name = (body.name or "").strip()[:100] or f"{src.name}{suffix}"[:100]
        cloned = CommentRule(
            platform=src.platform, name=new_name, mode=src.mode,
            account_id=target_acc_id, target_kind=src.target_kind,
            keyword=src.keyword, sec_uid=src.sec_uid, aweme_id=src.aweme_id,
            xsec_token=src.xsec_token, templates=src.templates,
            use_ai=src.use_ai, compose_mode=getattr(src, "compose_mode", ""),
            ai_channel_id=getattr(src, "ai_channel_id", ""),
            ai_model=getattr(src, "ai_model", ""),
            ai_preset_id=getattr(src, "ai_preset_id", ""),
            ai_prompt=getattr(src, "ai_prompt", ""),
            ai_max_chars=getattr(src, "ai_max_chars", 0),
            reply_filter=src.reply_filter, skip_keywords=src.skip_keywords,
            match_mode=getattr(src, "match_mode", "any"),
            include_keywords=getattr(src, "include_keywords", "[]"),
            exclude_keywords=getattr(src, "exclude_keywords", "[]"),
            comment_scope=getattr(src, "comment_scope", "all"),
            min_source_length=getattr(src, "min_source_length", 0),
            max_source_age_hours=getattr(src, "max_source_age_hours", 0),
            recent_works=getattr(src, "recent_works", 0),
            recent_days=getattr(src, "recent_days", 0),
            daily_cap=src.daily_cap, min_gap_seconds=src.min_gap_seconds,
            max_per_run=src.max_per_run, interval_seconds=src.interval_seconds,
            require_review=src.require_review, enabled=False,
        )
        s.add(cloned); s.commit(); s.refresh(cloned)
        return _rule_dict(cloned)


@router.post("/api/comment-rules/{rid}/preview")
async def preview_comment_rule_now(rid: int, limit: int = 8):
    """真试跑预览（Dry-Run）：不落库也不实际发送，返回筛选统计与原文↔生成文案对照。"""
    if not app_runtime.engine:
        raise HTTPException(503, "引擎未就绪")
    return await app_runtime.engine.preview_comment_rule(rid, limit=limit)


@router.post("/api/comment-rules/{rid}/run-now")
async def run_comment_rule_now(rid: int, as_draft: bool = False):
    if not app_runtime.engine:
        raise HTTPException(503, "引擎未就绪")
    return await app_runtime.engine.run_comment_rule(rid, force_draft=as_draft)


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


@router.post("/api/comment-tasks/{tid}/regenerate")
async def regenerate_comment_task_now(tid: int):
    """使用 AI 重新生成单条草稿/待发/失败任务的文案。"""
    if not app_runtime.engine:
        raise HTTPException(503, "引擎未就绪")
    res = await app_runtime.engine.regenerate_comment_task(tid)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "AI 重写失败")
    return res


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
        if not (t.content or "").strip():
            raise HTTPException(400, "评论文案为空，请先编辑文案或使用 AI 重写")
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
        t.content = content[:300]
        if getattr(t, "content_source", "") == "ai_empty_draft":
            t.content_source = "manual"
            t.ai_error = ""
        s.add(t); s.commit(); s.refresh(t)
        return metadata_service._task_dict(t)


def _approve_one(s, t) -> bool:
    """把 draft 任务转为 pending(通过审核)。空文案草稿拒绝通过。"""
    if t and t.status == "draft":
        if not (t.content or "").strip():
            return False
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
        if t.status != "draft":
            raise HTTPException(400, f"任务状态为 {t.status},非草稿")
        if not (t.content or "").strip():
            raise HTTPException(400, "该草稿文案为空（AI 生成失败），请先编辑或点「AI 重写」后再通过")
        _approve_one(s, t)
        s.commit()
    return {"ok": True}


class CommentBatchApproveIn(BaseModel):
    ids: list[int] = PydanticField(default_factory=list, max_length=500)
    platform: str
    account_id: int | None = None


@router.post("/api/comment-tasks/batch-approve")
async def batch_approve_comment_tasks(body: CommentBatchApproveIn):
    """Only approve an explicit, scope-checked selection, atomically."""
    if body.platform not in {"douyin", "xhs", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter", "facebook", "youtube"}:
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
        empty_ids = [t.id for t in rows if not (t.content or "").strip()]
        if empty_ids:
            raise HTTPException(400, f"有 {len(empty_ids)} 条草稿文案为空（AI 失败待填），请先编辑或取消勾选后再批量通过")
        for t in rows:
            if _approve_one(s, t):
                n += 1
        s.commit()
    return {"ok": True, "approved": n}


@router.post("/api/comment-tasks/batch-cancel")
async def batch_cancel_comment_tasks(body: IdsIn2):
    """批量弃用/取消选中的草稿、排队中或失败任务。"""
    ids = list(dict.fromkeys(body.ids or []))
    if not ids:
        raise HTTPException(422, "请先选择要取消的任务")
    n = 0
    with get_session() as s:
        rows = s.exec(select(CommentTask).where(CommentTask.id.in_(ids))).all()
        for t in rows:
            if t.status in ("draft", "pending", "failed"):
                t.status = "canceled"
                s.add(t); n += 1
        s.commit()
    return {"ok": True, "canceled": n}


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
