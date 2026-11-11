"""Persistent webhook outbox with signed, retryable delivery."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime

from sqlmodel import select

from .db import get_session
from .models import WebhookEvent, WebhookSubscription


def emit_event(event_type: str, payload: dict, *, aggregate_type: str = "",
               aggregate_id: str = "") -> int:
    with get_session() as session:
        event = WebhookEvent(
            event_type=str(event_type or "").strip(),
            aggregate_type=str(aggregate_type or "").strip(),
            aggregate_id=str(aggregate_id or "").strip(),
            payload_json=json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
        )
        session.add(event)
        session.commit()
        session.refresh(event)
        return int(event.id)


def _subscribed(subscription: WebhookSubscription, event_type: str) -> bool:
    try:
        values = json.loads(subscription.event_types or "[]")
    except (TypeError, ValueError):
        values = []
    return isinstance(values, list) and ("*" in values or event_type in values)


async def dispatch_event(event_id: int) -> dict:
    import httpx
    from .licensing import is_active
    if not is_active():
        return {"ok": False, "error": "license_required", "deferred": True}

    with get_session() as session:
        event = session.get(WebhookEvent, event_id)
        if event is None:
            return {"ok": False, "error": "事件不存在"}
        subscriptions = [row for row in session.exec(
            select(WebhookSubscription).where(WebhookSubscription.enabled == True)  # noqa:E712
        ).all() if _subscribed(row, event.event_type)]
        try:
            payload = json.loads(event.payload_json or "{}")
        except (TypeError, ValueError):
            payload = {}
        envelope = {
            "id": event.id,
            "type": event.event_type,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "created_at": event.created_at.isoformat() + "Z",
            "data": payload,
        }
        body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    delivered = 0
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        for subscription in subscriptions:
            signature = hmac.new(
                (subscription.secret or "").encode("utf-8"), body,
                hashlib.sha256).hexdigest()
            try:
                response = await client.post(
                    subscription.url, content=body,
                    headers={
                        "content-type": "application/json",
                        "x-creatorhub-event": event.event_type,
                        "x-creatorhub-delivery": str(event.id),
                        "x-creatorhub-signature-256": "sha256=" + signature,
                    })
                response.raise_for_status()
                delivered += 1
            except Exception as exc:
                errors.append(f"{subscription.name or subscription.id}: {exc}")

    with get_session() as session:
        event = session.get(WebhookEvent, event_id)
        if event is None:
            return {"ok": False, "error": "事件已被删除"}
        event.attempts += 1
        event.delivered_count = delivered
        event.last_error = "\n".join(errors)[-2000:]
        if not subscriptions or delivered == len(subscriptions):
            event.status = "delivered"
            event.delivered_at = datetime.utcnow()
        elif delivered:
            event.status = "partial"
        else:
            event.status = "failed"
        session.add(event)
        session.commit()
        return {
            "ok": not errors,
            "event_id": event.id,
            "status": event.status,
            "subscriptions": len(subscriptions),
            "delivered": delivered,
            "errors": errors,
        }


async def dispatch_pending(limit: int = 20) -> list[dict]:
    with get_session() as session:
        ids = list(session.exec(
            select(WebhookEvent.id)
            .where(WebhookEvent.status.in_(["pending", "partial", "failed"]))
            .where(WebhookEvent.attempts < 5)
            .order_by(WebhookEvent.created_at)
            .limit(max(1, min(int(limit or 20), 100)))
        ).all())
    return [await dispatch_event(int(event_id)) for event_id in ids]
