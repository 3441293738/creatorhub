"""私信会话与消息查询、实时事件流、历史记录拉取及私信自动回复规则接口。"""
from __future__ import annotations

from datetime import datetime
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import select

from app import runtime as app_runtime
from app.browser import fetch_dm_conversations, fetch_dm_history
from app.db import get_session
from app.models import DmAutoReplyRule, DmConversation, DmMessage, DmMonitorState, DouyinAccount
from app.platforms.douyin import DouyinClient, cookie_from_state as douyin_cookie_from_state
from app.platforms.instagram import InstagramClient
from app.platforms.twitter import TwitterClient
from app.risk import OperationKind
from app.submissions import submit_once
from app.transport_matrix import douyin_client_environment, resolve_transport

router = APIRouter()


# ─────────── 本账号管理:私信 ───────────
def _conv_dict(c: DmConversation) -> dict:
    return {
        "id": c.id, "platform": c.platform, "account_id": c.account_id,
        "conv_id": c.conv_id, "peer_uid": c.peer_uid, "peer_sec_uid": c.peer_sec_uid,
        "peer_nickname": c.peer_nickname, "peer_avatar": c.peer_avatar,
        "last_text": c.last_text, "last_time": c.last_time,
        "unread_count": c.unread_count,
        "fetched_at": c.fetched_at.isoformat() if c.fetched_at else None,
    }


@router.get("/api/dm/conversations")
async def list_dm_conversations(account_id: int, limit: int = 200):
    with get_session() as s:
        q = (select(DmConversation).where(DmConversation.account_id == account_id)
             .order_by(DmConversation.last_time.desc()).limit(limit))
        return [_conv_dict(c) for c in s.exec(q).all()]


@router.get("/api/dm/messages")
async def list_dm_messages(account_id: int, conv_id: str, limit: int = 200):
    with get_session() as s:
        q = (select(DmMessage).where(DmMessage.account_id == account_id,
                                     DmMessage.conv_id == conv_id)
             .order_by(DmMessage.create_time.asc()).limit(limit))

        def _card(m):
            if not m.raw_json:
                return None
            try:
                value = json.loads(m.raw_json)
                return value if isinstance(value, dict) and value.get("kind") else None
            except Exception:
                return None
        return [{"id": m.id, "direction": m.direction, "text": m.text,
                 "msg_type": m.msg_type, "create_time": m.create_time,
                 "card": _card(m)}
                for m in s.exec(q).all()]


@router.post("/api/accounts/{account_id}/dm/sync")
async def sync_dm(account_id: int):
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        platform = acc.platform
        dm_transport = (resolve_transport(app_runtime.cfg, platform, "dm_sync", acc)
                        if platform in {"douyin", "instagram", "twitter"} else None)
        if dm_transport and dm_transport["effective_mode"] == "unavailable":
            raise HTTPException(409, dm_transport["reason"] or "当前私信同步通道不可用")
        effective_mode = (dm_transport["effective_mode"]
                          if dm_transport else "browser")
        if app_runtime.browser is None and effective_mode == "browser":
            raise HTTPException(503, "浏览器未就绪")
        identity = (app_runtime.browser.identity_for(acc)
                    if app_runtime.browser is not None and effective_mode != "api" else None)
        state = acc.storage_state or acc.creator_storage_state or ""
        ua = acc.ua or app_runtime.cfg.engine.user_agent
        proxy = acc.proxy or ""
        ig_seed = acc.fp_seed or acc.profile_dir or ""
        ig_tz = acc.timezone_id or "Asia/Shanghai"
        direct_environment = douyin_client_environment(acc)
    if app_runtime.engine is None:
        raise HTTPException(503, "引擎未就绪")
    if platform == "xhs":
        result = await app_runtime.engine.poll_xhs_dm_now(account_id)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error") or "私信同步失败")
        cached_conversations = 0
        if result.get("skipped"):
            with get_session() as session:
                cached_conversations = len(session.exec(select(DmConversation.id).where(
                    DmConversation.account_id == account_id)).all())
        return {
            "ok": True,
            "fetched": int(result.get("conversations") or cached_conversations),
            "added": int(result.get("messages") or 0),
            "messages": int(result.get("messages") or 0),
            "queued": int(result.get("queued") or 0),
            "drafts": int(result.get("drafts") or 0),
            "skipped": bool(result.get("skipped")),
            "cached": bool(result.get("skipped") and cached_conversations),
            "reason": result.get("reason") or "",
        }
    source = "api" if effective_mode == "api" else "browser"

    async def _fetch_conversations():
        nonlocal source
        api_error = ""
        if platform == "douyin" and effective_mode in {"api", "hybrid"}:
            cookie = douyin_cookie_from_state(state)
            if not cookie:
                api_error = "api_missing_cookie"
            else:
                client = DouyinClient(
                    cookie, ua, timeout=app_runtime.cfg.engine.request_timeout_seconds,
                    proxy=proxy, **direct_environment)
                async with client.session_scope():
                    direct = await client.fetch_dm_conversations()
                api_error = client.last_error or ""
                if direct or not api_error:
                    source = "api"
                    return direct, ""
            if effective_mode == "api":
                source = "api"
                return [], api_error or "empty_response"
        elif platform == "instagram" and effective_mode in {"api", "hybrid"}:
            if not state:
                api_error = "logged_out:缺少 Instagram 登录态"
            else:
                icli = InstagramClient(
                    state,
                    ua,
                    proxy=proxy,
                    timeout=app_runtime.cfg.engine.request_timeout_seconds,
                    account_seed=ig_seed,
                    timezone_id=ig_tz,
                )
                async with icli.session_scope():
                    direct_ig, api_error = await icli.fetch_dm_conversations()
                if direct_ig or not api_error:
                    source = "api"
                    return direct_ig, ""
            if effective_mode == "api" or api_error.startswith(("logged_out:", "verification_required:")):
                source = "api"
                return [], api_error or "empty_response"
        elif platform == "twitter" and effective_mode in {"api", "hybrid"}:
            if not state:
                api_error = "logged_out:缺少 Twitter 登录态"
            else:
                tcli = TwitterClient(
                    state,
                    ua,
                    proxy=proxy,
                    timeout=app_runtime.cfg.engine.request_timeout_seconds,
                )
                async with tcli.session_scope():
                    direct_tw, api_error = await tcli.fetch_dm_inbox()
                if direct_tw or not api_error:
                    source = "api"
                    return direct_tw, ""
            if effective_mode == "api" or api_error.startswith(("logged_out:", "verification_required:")):
                source = "api"
                return [], api_error or "empty_response"
        if app_runtime.browser is None or identity is None:
            return [], api_error or "browser_unavailable"
        source = "browser_fallback" if effective_mode == "hybrid" else "browser"
        conversations, browser_error = await fetch_dm_conversations(
            app_runtime.browser, identity, platform)
        return conversations, browser_error or api_error

    convs, err = await app_runtime.engine.guarded_read_pair(
        account_id, OperationKind.READ_HEAVY, f"dm:{account_id}",
        _fetch_conversations, empty_result=[])
    if err.startswith("risk_deferred:"):
        return {"ok": True, "fetched": 0, "added": 0, "skipped": True,
                "reason": err.split(":", 1)[-1]}
    if err and err.startswith("logged_out"):
        raise HTTPException(400, "登录态已失效,请点「重新登录」")
    # 小红书网页端私信未开放(entry visible=false)等硬限制:直接把原因回给前端
    if not convs and err:
        raise HTTPException(400, err)
    now = datetime.utcnow()
    with get_session() as s:
        # 快照式替换:清掉旧会话(含历史误抓的 JS 模块垃圾),写入本次抓到的
        for old in s.exec(select(DmConversation).where(
                DmConversation.account_id == account_id)).all():
            s.delete(old)
        # 会话最后一条消息也快照式重写(仅 last:<conv> 这条,历史记录由按需抓取补)
        for old in s.exec(select(DmMessage).where(
                DmMessage.account_id == account_id,
                DmMessage.msg_id.like("last:%"))).all():
            s.delete(old)
        msgs = 0
        for c in convs:
            s.add(DmConversation(platform=platform, account_id=account_id,
                                 fetched_at=now, **c))
            # get_message_by_init 已带每会话最后一条消息:落成 thread 里的一条,
            # 让「点开会话」不再空。方向由 last_sender_uid==self_uid 判定。
            meta = {}
            try:
                meta = json.loads(c.get("raw_json") or "{}")
            except Exception:
                meta = {}
            if c.get("last_text"):
                direction = ("out" if meta.get("last_sender_uid")
                             and meta.get("last_sender_uid") == meta.get("self_uid")
                             else "in")
                s.add(DmMessage(
                    platform=platform, account_id=account_id, conv_id=c["conv_id"],
                    msg_id="last:" + c["conv_id"], direction=direction,
                    msg_type="text", text=c["last_text"],
                    create_time=c.get("last_time") or 0))
                msgs += 1
        # 顺带存账号自身 uid(= IM device_id,实时接收 WS 要用);从任一会话的 self_uid 取
        self_uid = ""
        for c in convs:
            try:
                self_uid = (json.loads(c.get("raw_json") or "{}")).get("self_uid", "")
            except Exception:
                self_uid = ""
            if self_uid:
                break
        if self_uid:
            acc2 = s.get(DouyinAccount, account_id)
            if acc2 and acc2.uid != self_uid:
                acc2.uid = self_uid
                s.add(acc2)
        s.commit()
    return {"ok": True, "fetched": len(convs), "added": len(convs),
            "messages": msgs, "source": source,
            "configured_mode": (dm_transport["configured_mode"]
                                if dm_transport else "browser")}


@router.get("/api/dm/stream")
async def dm_stream(account_id: int):
    """私信实时事件流(SSE)。前端打开 DM 面板时订阅;订阅即为该账号拉起 frontier-im
    WS 长连接,新消息实时推来(也已入库)。最后一个订阅断开时自动停连。"""
    if app_runtime.im_receiver is None:
        raise HTTPException(503, "实时接收未就绪")
    q = await app_runtime.im_receiver.subscribe(account_id)

    async def gen():
        try:
            yield "retry: 3000\nevent: ready\ndata: {}\n\n"
            while True:
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"     # 心跳,防代理断流
        except asyncio.CancelledError:
            pass
        finally:
            app_runtime.im_receiver.unsubscribe(account_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/api/accounts/{account_id}/dm/conversations/{conv_id:path}/mark-read")
async def mark_dm_read(account_id: int, conv_id: str):
    """标记会话已读:清本地未读计数(红点)。"""
    with get_session() as s:
        conv = s.exec(select(DmConversation).where(
            DmConversation.account_id == account_id,
            DmConversation.conv_id == conv_id)).first()
        if conv and conv.unread_count:
            conv.unread_count = 0
            s.add(conv); s.commit()
    return {"ok": True}


@router.post("/api/accounts/{account_id}/dm/conversations/{conv_id:path}/fetch-history")
async def fetch_dm_conversation_history(account_id: int, conv_id: str,
                                        cursor: int = 0, debug: bool = False):
    """无头抓单个会话历史消息(imapi get_by_conversation,纯 cookie),落库 DmMessage。"""
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        platform = acc.platform
        ig_mode = str(getattr(app_runtime.cfg.engine, "instagram_read_mode", "hybrid") or "hybrid").lower()
        tw_mode = str(getattr(app_runtime.cfg.engine, "twitter_read_mode", "hybrid") or "hybrid").lower()
        if app_runtime.browser is None and not (
            (platform == "instagram" and ig_mode in {"api", "hybrid"})
            or (platform == "twitter" and tw_mode in {"api", "hybrid"})
        ):
            raise HTTPException(503, "浏览器未就绪")
        identity = app_runtime.browser.identity_for(acc) if app_runtime.browser is not None else None
        acc_state = acc.storage_state or acc.creator_storage_state or acc.cookie or ""
        acc_ua = acc.ua or app_runtime.cfg.engine.user_agent
        acc_proxy = acc.proxy or ""
        acc_seed = acc.fp_seed or acc.profile_dir or ""
        acc_tz = acc.timezone_id or "Asia/Shanghai"
        conv = s.exec(select(DmConversation).where(
            DmConversation.account_id == account_id,
            DmConversation.conv_id == conv_id)).first()
        if not conv:
            raise HTTPException(404, "会话不存在(先同步会话列表)")
        short_id, self_uid = conv.conv_short_id, ""
        try:
            self_uid = (json.loads(conv.raw_json or "{}")).get("self_uid", "")
        except Exception:
            pass
    if platform == "douyin" and not short_id:
        raise HTTPException(400, "该会话缺 conversation_short_id,请重新同步会话列表")
    if app_runtime.engine is None:
        raise HTTPException(503, "引擎未就绪")
    async def _fetch_history():
        if platform == "instagram" and ig_mode in {"api", "hybrid"} and acc_state:
            icli = InstagramClient(
                acc_state,
                acc_ua,
                proxy=acc_proxy,
                timeout=app_runtime.cfg.engine.request_timeout_seconds,
                account_seed=acc_seed,
                timezone_id=acc_tz,
            )
            async with icli.session_scope():
                msgs_ig, err_ig = await icli.fetch_dm_history(conv_id)
            if msgs_ig or not err_ig or ig_mode == "api" or app_runtime.browser is None:
                return {"messages": msgs_ig, "next_cursor": "", "has_more": False}, err_ig
        if platform == "twitter" and tw_mode in {"api", "hybrid"} and acc_state:
            tcli = TwitterClient(
                acc_state,
                acc_ua,
                proxy=acc_proxy,
                timeout=app_runtime.cfg.engine.request_timeout_seconds,
            )
            async with tcli.session_scope():
                msgs_tw, err_tw = await tcli.fetch_dm_messages(conv_id)
            if msgs_tw or not err_tw or tw_mode == "api" or app_runtime.browser is None:
                return {"messages": msgs_tw, "next_cursor": "", "has_more": False}, err_tw
        return await fetch_dm_history(
            app_runtime.browser, identity, platform, conv_id, short_id,
            conv_type=1, cursor=cursor, debug=debug,
            peer_uid=conv.peer_uid, self_uid=self_uid)
    read_guard = (app_runtime.engine.guarded_interactive_read_pair if platform == "xhs"
                  else app_runtime.engine.guarded_read_pair)
    parsed, err = await read_guard(
        account_id, OperationKind.READ_HEAVY,
        f"dm-history:{account_id}:{conv_id}", _fetch_history,
        empty_result={})
    if err.startswith("risk_deferred:"):
        return {"ok": True, "messages": 0, "added": 0, "skipped": True,
                "reason": err.split(":", 1)[-1]}
    if err:
        raise HTTPException(400, err)
    msgs = parsed.get("messages", [])
    added = 0
    with get_session() as s:
        # 按会话快照重写:拉到消息就清掉该会话旧消息(含 last:<conv> 占位、旧错时间戳),
        # 再插本次窗口。get_by_conversation 每次返回最近一窗,快照式最简单且能纠正旧数据。
        if msgs and platform != "xhs":
            for old in s.exec(select(DmMessage).where(
                    DmMessage.account_id == account_id,
                    DmMessage.conv_id == conv_id)).all():
                s.delete(old)
        seen = set()
        for m in msgs:
            mid = m.get("server_msg_id") or ""
            if not mid or mid in seen:
                continue
            seen.add(mid)
            direction = (str(m.get("direction") or "") if platform in ("xhs", "bilibili", "instagram", "twitter") else
                         "out" if self_uid and m.get("sender_uid") == self_uid else "in")
            card = m.get("card")
            existing = s.exec(select(DmMessage).where(
                DmMessage.account_id == account_id,
                DmMessage.conv_id == conv_id,
                DmMessage.msg_id == mid)).first()
            if existing is None:
                existing = DmMessage(platform=platform, account_id=account_id,
                                     conv_id=conv_id, msg_id=mid)
                added += 1
            existing.direction = direction
            existing.msg_type = ("video" if card else
                                 "text" if m.get("text") else str(m.get("msg_type") or ""))
            existing.text = m.get("text") or ""
            existing.create_time = int(m.get("create_time") or 0)
            if platform == "xhs":
                existing.raw_json = json.dumps({
                    "store_id": int(m.get("store_id") or 0),
                    "sender_uid": m.get("sender_uid") or "",
                    "receiver_uid": m.get("receiver_uid") or "",
                    "group_chat": bool(m.get("group_chat")),
                    "content": m.get("content"),
                }, ensure_ascii=False)
            else:
                existing.raw_json = json.dumps(card, ensure_ascii=False) if card else ""
            s.add(existing)
        s.commit()
    out = {"ok": True, "fetched": len(msgs), "added": added,
           "next_cursor": parsed.get("next_cursor"), "has_more": parsed.get("has_more")}
    if debug:   # 非文本消息(分享视频=8/图片=27/语音=17...)回原始 content,标定字段用
        out["media_samples"] = [
            {"msg_type": m.get("msg_type"), "text": m.get("text"),
             "content": m.get("content")}
            for m in msgs if m.get("msg_type") not in (7, 0)]
    return out


class DmAutoReplyRuleIn(BaseModel):
    account_id: int
    name: str = "自动回复"
    enabled: bool = True
    match_mode: str = "keywords"
    keywords: list[str] = PydanticField(default_factory=list)
    exclude_keywords: list[str] = PydanticField(default_factory=list)
    reply_templates: list[str] = PydanticField(default_factory=list)
    review_before_send: bool = True
    min_delay_seconds: int = 75
    max_delay_seconds: int = 300
    cooldown_seconds: int = 21600
    max_message_age_seconds: int = 1800


def _clean_string_list(values: list[str], *, limit: int, item_limit: int) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values[:limit]:
        text = " ".join(str(value or "").strip().split())[:item_limit]
        key = text.casefold()
        if text and key not in seen:
            seen.add(key); cleaned.append(text)
    return cleaned


def _dm_rule_dict(rule: DmAutoReplyRule) -> dict:
    def load(raw: str) -> list:
        try:
            value = json.loads(raw or "[]")
            return value if isinstance(value, list) else []
        except Exception:
            return []
    return {
        "id": rule.id, "platform": rule.platform, "account_id": rule.account_id,
        "name": rule.name, "enabled": rule.enabled, "match_mode": rule.match_mode,
        "keywords": load(rule.keywords),
        "exclude_keywords": load(rule.exclude_keywords),
        "reply_templates": load(rule.reply_templates),
        "review_before_send": rule.review_before_send,
        "min_delay_seconds": rule.min_delay_seconds,
        "max_delay_seconds": rule.max_delay_seconds,
        "cooldown_seconds": rule.cooldown_seconds,
        "max_message_age_seconds": rule.max_message_age_seconds,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
        "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
    }


def _apply_rule_input(rule: DmAutoReplyRule, body: DmAutoReplyRuleIn) -> None:
    if body.match_mode not in {"all", "keywords"}:
        raise HTTPException(400, "match_mode 仅支持 all | keywords")
    keywords = _clean_string_list(body.keywords, limit=30, item_limit=80)
    excludes = _clean_string_list(body.exclude_keywords, limit=30, item_limit=80)
    templates = _clean_string_list(body.reply_templates, limit=20, item_limit=500)
    if body.match_mode == "keywords" and not keywords:
        raise HTTPException(400, "关键词模式至少填写一个关键词")
    if not templates:
        raise HTTPException(400, "至少填写一条回复模板")
    rule.name = str(body.name or "自动回复").strip()[:80]
    rule.enabled = bool(body.enabled)
    rule.match_mode = body.match_mode
    rule.keywords = json.dumps(keywords, ensure_ascii=False)
    rule.exclude_keywords = json.dumps(excludes, ensure_ascii=False)
    rule.reply_templates = json.dumps(templates, ensure_ascii=False)
    rule.review_before_send = bool(body.review_before_send)
    rule.min_delay_seconds = max(15, min(3600, int(body.min_delay_seconds)))
    rule.max_delay_seconds = max(
        rule.min_delay_seconds, min(7200, int(body.max_delay_seconds)))
    rule.cooldown_seconds = max(300, min(604800, int(body.cooldown_seconds)))
    rule.max_message_age_seconds = max(
        60, min(86400, int(body.max_message_age_seconds)))
    rule.updated_at = datetime.utcnow()


@router.get("/api/dm/auto-reply-rules")
async def list_dm_auto_reply_rules(account_id: int):
    with get_session() as session:
        rows = session.exec(select(DmAutoReplyRule).where(
            DmAutoReplyRule.account_id == account_id
        ).order_by(DmAutoReplyRule.id.asc())).all()
        return [_dm_rule_dict(row) for row in rows]


@router.post("/api/dm/auto-reply-rules")
async def create_dm_auto_reply_rule(body: DmAutoReplyRuleIn, request: Request = None):
    with get_session() as session:
        def create():
            account = session.get(DouyinAccount, body.account_id)
            if not account or account.platform != "xhs":
                raise HTTPException(404, "小红书账号不存在")
            rule = DmAutoReplyRule(account_id=body.account_id, platform="xhs")
            _apply_rule_input(rule, body)
            session.add(rule); session.flush()
            return _dm_rule_dict(rule)
        payload, _ = submit_once(session, request=request, scope="dm-rule", body=body, create=create)
        return payload


@router.put("/api/dm/auto-reply-rules/{rule_id}")
async def update_dm_auto_reply_rule(rule_id: int, body: DmAutoReplyRuleIn):
    with get_session() as session:
        rule = session.get(DmAutoReplyRule, rule_id)
        if not rule:
            raise HTTPException(404, "规则不存在")
        if rule.account_id != body.account_id:
            raise HTTPException(400, "规则与账号不匹配")
        _apply_rule_input(rule, body)
        session.add(rule); session.commit(); session.refresh(rule)
        return _dm_rule_dict(rule)


@router.delete("/api/dm/auto-reply-rules/{rule_id}")
async def delete_dm_auto_reply_rule(rule_id: int):
    with get_session() as session:
        rule = session.get(DmAutoReplyRule, rule_id)
        if not rule:
            raise HTTPException(404, "规则不存在")
        session.delete(rule); session.commit()
    return {"ok": True}


@router.post("/api/accounts/{account_id}/dm/automation/poll-now")
async def poll_xhs_dm_automation_now(account_id: int):
    if app_runtime.engine is None:
        raise HTTPException(503, "引擎未就绪")
    result = await app_runtime.engine.poll_xhs_dm_now(account_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "私信检查失败")
    return result


@router.get("/api/accounts/{account_id}/dm/automation/status")
async def xhs_dm_automation_status(account_id: int):
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if not account or account.platform != "xhs":
            raise HTTPException(404, "小红书账号不存在")
        state = session.exec(select(DmMonitorState).where(
            DmMonitorState.account_id == account_id,
            DmMonitorState.platform == "xhs",
        )).first()
        enabled_rules = len(session.exec(select(DmAutoReplyRule.id).where(
            DmAutoReplyRule.account_id == account_id,
            DmAutoReplyRule.enabled == True,  # noqa: E712
        )).all())
    runtime = (app_runtime.engine.dm_automation.realtime_status(account_id)
               if app_runtime.engine is not None else {})
    return {
        "ok": True,
        "scope": "account",
        "new_conversations": True,
        "monitor_enabled": bool(app_runtime.cfg.engine.xhs_dm_monitor_enabled),
        "auto_reply_enabled": bool(app_runtime.cfg.engine.xhs_dm_auto_reply_enabled),
        "enabled_rules": enabled_rules,
        "baseline_initialized": bool(state and state.baseline_initialized),
        "baseline_at": state.baseline_at.isoformat() if state and state.baseline_at else None,
        "last_poll_at": state.last_poll_at.isoformat() if state and state.last_poll_at else None,
        "last_push_at": state.last_push_at.isoformat() if state and state.last_push_at else None,
        "last_error": state.last_error if state else "",
        "fallback_interval_seconds": int(app_runtime.cfg.engine.xhs_dm_fallback_interval_seconds),
        "realtime": runtime,
    }
