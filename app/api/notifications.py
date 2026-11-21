"""消息通知渠道配置测试与 Webhook 订阅及事件投递管理接口。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict
from urllib.parse import urlsplit
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import select

from app.db import get_session
from app.event_center import dispatch_event, dispatch_pending
from app.models import NotificationChannel, WebhookEvent, WebhookSubscription
from app.notification_config import merge_config, parse_config, redact_config, redact_detail
from app.notifier import CHANNEL_TYPES, send_one
from app.services import collections as collections_service
from app.services import metadata as metadata_service

router = APIRouter()


class WebhookSubscriptionIn(BaseModel):
    name: str = ""
    url: str
    secret: str = ""
    event_types: list[str] = PydanticField(default_factory=lambda: ["*"])
    enabled: bool = True


class WebhookSubscriptionUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    secret: str | None = None
    event_types: list[str] | None = None
    enabled: bool | None = None


def _validate_webhook_url(value: str) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise HTTPException(422, "Webhook URL 无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(422, "Webhook URL 必须是完整的 HTTP(S) 地址")
    if parsed.username or parsed.password or len(value) > 2000:
        raise HTTPException(422, "Webhook URL 不能包含账号密码且长度须合理")
    return value


def _webhook_subscription_dict(row: WebhookSubscription) -> dict:
    return {"id": row.id, "name": row.name, "url": row.url,
            "secret_configured": bool(row.secret),
            "event_types": json.loads(row.event_types or "[]"),
            "enabled": row.enabled, "created_at": metadata_service._queue_iso(row.created_at),
            "updated_at": metadata_service._queue_iso(row.updated_at)}


@router.get("/api/webhooks")
async def list_webhooks():
    with get_session() as session:
        rows = session.exec(select(WebhookSubscription).order_by(
            WebhookSubscription.created_at.desc())).all()
    return [_webhook_subscription_dict(row) for row in rows]


@router.post("/api/webhooks")
async def create_webhook(body: WebhookSubscriptionIn):
    with get_session() as session:
        row = WebhookSubscription(
            name=body.name.strip() or "Webhook", url=_validate_webhook_url(body.url),
            secret=body.secret, event_types=json.dumps(
                collections_service._collection_keywords(body.event_types) or ["*"]), enabled=body.enabled)
        session.add(row); session.commit(); session.refresh(row)
        return _webhook_subscription_dict(row)


@router.patch("/api/webhooks/{webhook_id}")
async def update_webhook(webhook_id: int, body: WebhookSubscriptionUpdate):
    with get_session() as session:
        row = session.get(WebhookSubscription, webhook_id)
        if not row:
            raise HTTPException(404, "Webhook 不存在")
        if body.name is not None:
            row.name = body.name.strip()[:120]
        if body.url is not None:
            row.url = _validate_webhook_url(body.url)
        if body.secret is not None:
            row.secret = body.secret
        if body.event_types is not None:
            row.event_types = json.dumps(collections_service._collection_keywords(body.event_types) or ["*"])
        if body.enabled is not None:
            row.enabled = body.enabled
        row.updated_at = datetime.utcnow()
        session.add(row); session.commit(); session.refresh(row)
        return _webhook_subscription_dict(row)


@router.delete("/api/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: int):
    with get_session() as session:
        row = session.get(WebhookSubscription, webhook_id)
        if row:
            session.delete(row); session.commit()
    return {"ok": True}


@router.get("/api/webhook-events")
async def list_webhook_events(status: str = "", limit: int = 200):
    with get_session() as session:
        stmt = select(WebhookEvent)
        if status:
            stmt = stmt.where(WebhookEvent.status == status)
        rows = session.exec(stmt.order_by(
            WebhookEvent.created_at.desc()).limit(max(1, min(limit, 1000)))).all()
    return [{"id": row.id, "event_type": row.event_type,
             "aggregate_type": row.aggregate_type, "aggregate_id": row.aggregate_id,
             "payload": json.loads(row.payload_json or "{}"), "status": row.status,
             "attempts": row.attempts, "delivered_count": row.delivered_count,
             "last_error": row.last_error, "created_at": metadata_service._queue_iso(row.created_at),
             "delivered_at": metadata_service._queue_iso(row.delivered_at)} for row in rows]


@router.post("/api/webhook-events/{event_id}/dispatch")
async def dispatch_webhook_event(event_id: int):
    result = await dispatch_event(event_id)
    if not result.get("ok") and result.get("error") == "事件不存在":
        raise HTTPException(404, result["error"])
    return result


@router.post("/api/webhook-events/dispatch-pending")
async def dispatch_pending_webhook_events(limit: int = 20):
    results = await dispatch_pending(limit)
    return {"ok": all(value.get("ok") for value in results), "results": results}


# ─────────── 通知渠道 ───────────
class ChannelIn(BaseModel):
    name: str = ""
    type: str
    config: Dict[str, Any] = {}
    enabled: bool = True


class ChannelUpdate(BaseModel):
    name: str | None = None
    config: Dict[str, Any] | None = None
    enabled: bool | None = None


def _channel_dict(c: NotificationChannel) -> dict:
    config = parse_config(c.config)
    return {"id": c.id, "name": c.name, "type": c.type,
            "enabled": c.enabled, "config": redact_config(c.type, config)}


@router.get("/api/notifications")
async def list_channels():
    with get_session() as s:
        return [_channel_dict(c) for c in s.exec(select(NotificationChannel)).all()]


@router.post("/api/notifications")
async def add_channel(body: ChannelIn):
    if body.type not in CHANNEL_TYPES:
        raise HTTPException(400, f"渠道类型须为 {CHANNEL_TYPES}")
    with get_session() as s:
        c = NotificationChannel(name=body.name or body.type, type=body.type,
                                config=json.dumps(merge_config({}, body.config)), enabled=body.enabled)
        s.add(c); s.commit(); s.refresh(c)
        return _channel_dict(c)


@router.put("/api/notifications/{cid}")
async def update_channel(cid: int, body: ChannelUpdate):
    with get_session() as s:
        c = s.get(NotificationChannel, cid)
        if not c:
            raise HTTPException(404)
        if body.name is not None:
            c.name = body.name
        if body.config is not None:
            c.config = json.dumps(merge_config(parse_config(c.config), body.config))
        if body.enabled is not None:
            c.enabled = body.enabled
        s.add(c); s.commit(); s.refresh(c)
        return _channel_dict(c)


@router.delete("/api/notifications/{cid}")
async def del_channel(cid: int):
    with get_session() as s:
        c = s.get(NotificationChannel, cid)
        if c:
            s.delete(c); s.commit()
    return {"ok": True}


@router.post("/api/notifications/{cid}/test")
async def test_channel(cid: int):
    with get_session() as s:
        c = s.get(NotificationChannel, cid)
        if not c:
            raise HTTPException(404)
        ch_type, cfg = c.type, json.loads(c.config or "{}")
    ok, detail = await send_one(ch_type, cfg, "CreatorHub · 测试通知",
                                "这是一条测试消息,收到说明渠道配置正常 ✓")
    return {"ok": ok, "detail": redact_detail(ch_type, cfg, detail)}
