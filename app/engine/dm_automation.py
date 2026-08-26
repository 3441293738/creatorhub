"""Conservative XHS Web-DM synchronization and rule-based reply queueing."""
from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from sqlmodel import select

from ..browser.xhs_im import (
    CHAT_URL,
    _background_page,
    fetch_conversations_in_page,
    fetch_history_in_page,
)
from ..db import get_session
from ..models import (
    AccountActionTask,
    DmAutoReplyRule,
    DmConversation,
    DmMessage,
    DmMonitorState,
    DouyinAccount,
)


def _json_dict(value: str) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _json_list(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except Exception:
        parsed = []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def rule_matches(rule: DmAutoReplyRule, text: str) -> bool:
    folded = str(text or "").strip().casefold()
    if not folded:
        return False
    if any(term.casefold() in folded for term in _json_list(rule.exclude_keywords)):
        return False
    mode = str(rule.match_mode or "keywords").strip().lower()
    if mode == "all":
        return True
    terms = _json_list(rule.keywords)
    return bool(terms) and any(term.casefold() in folded for term in terms)


def render_reply(rule: DmAutoReplyRule, *, nickname: str, message: str) -> str:
    templates = _json_list(rule.reply_templates)
    if not templates:
        return ""
    template = random.choice(templates)
    values = {
        "nickname": str(nickname or "朋友"),
        "message": str(message or "")[:120],
        "hour": str(datetime.now().hour),
    }
    try:
        rendered = template.format_map(values)
    except (KeyError, ValueError):
        rendered = template
    return " ".join(str(rendered).strip().split())[:500]


def _max_store(raw_json: str) -> int:
    try:
        return max(0, int(_json_dict(raw_json).get("max_store_id") or 0))
    except (TypeError, ValueError):
        return 0


class XhsDmAutomation:
    def __init__(self, cfg: Any, browser: Any):
        self.cfg = cfg
        self.browser = browser
        self._next_poll: dict[int, float] = {}
        self._observer_pages: dict[int, Any] = {}
        self._observer_callbacks: dict[int, Any] = {}
        self._active_sockets: dict[int, int] = {}
        self._realtime_status: dict[int, dict[str, Any]] = {}
        self._wake_tasks: dict[int, asyncio.Task] = {}
        self._last_poll_monotonic: dict[int, float] = {}
        self._wake_callback: Callable[[int, str], Awaitable[Any]] | None = None

    def set_wake_callback(
            self, callback: Callable[[int, str], Awaitable[Any]] | None) -> None:
        self._wake_callback = callback

    def realtime_status(self, account_id: int) -> dict:
        current = dict(self._realtime_status.get(account_id) or {})
        current.setdefault("mode", "realtime" if bool(
            self.cfg.engine.xhs_dm_realtime_enabled) else "polling")
        current.setdefault("connected", False)
        current.setdefault("state", "waiting")
        current.setdefault("last_push_at", None)
        current["next_fallback_at"] = self._next_poll.get(account_id)
        return current

    def due(self, account_id: int, *, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self._next_poll.get(account_id, 0)

    def postpone(self, account_id: int, *, now: float | None = None) -> None:
        connected = bool((self._realtime_status.get(account_id) or {}).get("connected"))
        base = max(30, int(
            self.cfg.engine.xhs_dm_fallback_interval_seconds
            if connected and bool(self.cfg.engine.xhs_dm_realtime_enabled)
            else self.cfg.engine.xhs_dm_poll_interval_seconds))
        jitter = min(1.0, max(0.0, float(self.cfg.engine.xhs_dm_poll_jitter)))
        delay = base * random.uniform(1.0, 1.0 + jitter)
        self._next_poll[account_id] = (now if now is not None else time.time()) + delay

    @staticmethod
    def _business_push(payload: Any) -> bool:
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except Exception:
                return True
        try:
            frame = json.loads(str(payload or ""))
        except Exception:
            return bool(payload)
        if not isinstance(frame, dict) or int(frame.get("t") or 0) == 0:
            return False
        # Connection/register acknowledgements live under b.a. Actual inbound
        # application deliveries use b.d; the IM body itself is protobuf.
        return isinstance(frame.get("b"), dict) and "d" in frame["b"]

    def _schedule_wake(self, account_id: int, reason: str, *, delay: float) -> None:
        if self._wake_callback is None:
            return
        previous = self._wake_tasks.get(account_id)
        if previous is not None and not previous.done():
            if reason == "push":
                previous.cancel()
            else:
                return
        event_at = time.monotonic()

        async def run() -> None:
            try:
                await asyncio.sleep(max(0.1, delay))
                if reason == "reconnect" and bool(
                        (self._realtime_status.get(account_id) or {}).get("connected")):
                    return
                # A concurrent poll that completed after this frame already
                # included the new frontier; avoid a duplicate HTTP read.
                if reason == "push" and self._last_poll_monotonic.get(
                        account_id, 0.0) >= event_at:
                    return
                await self._wake_callback(account_id, reason)
            except asyncio.CancelledError:
                pass
            finally:
                if self._wake_tasks.get(account_id) is asyncio.current_task():
                    self._wake_tasks.pop(account_id, None)

        self._wake_tasks[account_id] = asyncio.create_task(run())

    async def ensure_realtime(self, account: DouyinAccount) -> bool:
        """Attach to the page's native account-wide push socket once."""
        account_id = int(account.id or 0)
        if not account_id:
            return False
        enabled = bool(self.cfg.engine.xhs_dm_realtime_enabled)
        identity = self.browser.identity_for(account)
        async with _background_page(self.browser, identity) as page:
            previous = self._observer_pages.get(account_id)
            attached = False
            if enabled and previous is not page:
                old_callback = self._observer_callbacks.get(account_id)
                if previous is not None and old_callback is not None:
                    try:
                        previous.remove_listener("websocket", old_callback)
                    except Exception:
                        pass

                def on_websocket(socket: Any) -> None:
                    url = str(getattr(socket, "url", "") or "")
                    if "apppush-rws.xiaohongshu.com/rwp" not in url:
                        return
                    socket_id = id(socket)
                    self._active_sockets[account_id] = socket_id
                    status = self._realtime_status.setdefault(account_id, {})
                    status.update({"mode": "realtime", "connected": True,
                                   "state": "connected", "url": url})

                    def on_frame(payload: Any) -> None:
                        status["connected"] = True
                        status["state"] = "connected"
                        if not self._business_push(payload):
                            return
                        now = datetime.utcnow()
                        status["last_push_at"] = now.isoformat()
                        self._schedule_wake(
                            account_id, "push",
                            delay=float(self.cfg.engine.xhs_dm_realtime_debounce_seconds))

                    def on_close(*_args: Any) -> None:
                        if self._active_sockets.get(account_id) != socket_id:
                            return
                        status["connected"] = False
                        status["state"] = "reconnecting"
                        self._schedule_wake(account_id, "reconnect", delay=5.0)

                    socket.on("framereceived", on_frame)
                    socket.on("close", on_close)

                page.on("websocket", on_websocket)
                self._observer_pages[account_id] = page
                self._observer_callbacks[account_id] = on_websocket
                attached = True
                self._realtime_status[account_id] = {
                    "mode": "realtime", "connected": False,
                    "state": "connecting", "last_push_at": None,
                }
            current_url = str(getattr(page, "url", "") or "")
            if "xiaohongshu.com/chat" not in current_url:
                await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
                await self.browser.xhs_interaction.pause(0.25, 0.5)
            elif attached:
                # The socket may have been created before this process attached
                # to a restored tab. One controlled reload establishes an
                # observable connection; later polls are navigation-free.
                await page.reload(wait_until="domcontentloaded", timeout=30000)
                await self.browser.xhs_interaction.pause(0.25, 0.5)
            return enabled

    async def stop(self) -> None:
        for task in list(self._wake_tasks.values()):
            task.cancel()
        if self._wake_tasks:
            await asyncio.gather(*self._wake_tasks.values(), return_exceptions=True)
        self._wake_tasks.clear()
        for account_id, page in list(self._observer_pages.items()):
            callback = self._observer_callbacks.get(account_id)
            if callback is not None:
                try:
                    page.remove_listener("websocket", callback)
                except Exception:
                    pass
        self._observer_pages.clear()
        self._observer_callbacks.clear()

    async def poll(self, account: DouyinAccount, *, trigger: str = "scheduled") -> dict:
        account_id = int(account.id or 0)
        identity = self.browser.identity_for(account)
        await self.ensure_realtime(account)
        with get_session() as session:
            monitor = session.exec(select(DmMonitorState).where(
                DmMonitorState.account_id == account_id,
                DmMonitorState.platform == "xhs",
            )).first()
            if monitor is None:
                existing = session.exec(select(DmConversation.id).where(
                    DmConversation.account_id == account_id)).first()
                monitor = DmMonitorState(
                    account_id=account_id, platform="xhs",
                    baseline_initialized=existing is not None,
                    baseline_at=datetime.utcnow() if existing is not None else None,
                )
                session.add(monitor); session.commit(); session.refresh(monitor)
            global_baseline = not bool(monitor.baseline_initialized)
        async with _background_page(self.browser, identity) as page:
            convs, error = await fetch_conversations_in_page(
                page, pages=5 if global_baseline else 1)
            if error:
                with get_session() as session:
                    row = session.get(DmMonitorState, monitor.id)
                    if row:
                        row.last_error = error[:500]
                        row.updated_at = datetime.utcnow()
                        session.add(row); session.commit()
                return {"ok": False, "error": error, "conversations": 0,
                        "messages": 0, "queued": 0, "drafts": 0}

            changed: list[tuple[dict, int, bool]] = []
            now = datetime.utcnow()
            with get_session() as session:
                for item in convs:
                    conv_id = str(item.get("conv_id") or "")
                    if not conv_id:
                        continue
                    old = session.exec(select(DmConversation).where(
                        DmConversation.account_id == account_id,
                        DmConversation.conv_id == conv_id,
                    )).first()
                    old_max = _max_store(old.raw_json) if old else 0
                    new_max = _max_store(str(item.get("raw_json") or ""))
                    if old is None:
                        old = DmConversation(
                            platform="xhs", account_id=account_id, conv_id=conv_id)
                    for field in (
                        "peer_uid", "peer_sec_uid", "peer_nickname", "peer_avatar",
                        "last_text", "last_time", "unread_count", "conv_short_id",
                        "ticket",
                    ):
                        setattr(old, field, item.get(field) or (0 if field in {
                            "last_time", "unread_count"} else ""))
                    old.fetched_at = now
                    if global_baseline or new_max <= old_max:
                        old.raw_json = str(item.get("raw_json") or "")
                    session.add(old)
                    if new_max > old_max:
                        changed.append((item, old_max, global_baseline))
                session.commit()

            updates = [entry for entry in changed if not entry[2]]
            updates = updates[:max(1, int(
                self.cfg.engine.xhs_dm_max_conversations_per_poll))]
            message_count = queued = drafts = 0
            events: list[dict] = []
            for item, old_max, _ in updates:
                meta = _json_dict(str(item.get("raw_json") or ""))
                new_max = int(meta.get("max_store_id") or 0)
                parsed, hist_error = await fetch_history_in_page(
                    page, str(item["conv_id"]),
                    peer_uid=str(item.get("peer_uid") or item["conv_id"]),
                    self_uid=str(meta.get("self_uid") or account.uid or ""),
                    last_id=new_max, start_id=old_max, count=50)
                if hist_error:
                    continue
                incoming_ids: list[str] = []
                with get_session() as session:
                    for msg in parsed.get("messages", []):
                        msg_id = str(msg.get("server_msg_id") or "")
                        if not msg_id:
                            continue
                        row = session.exec(select(DmMessage).where(
                            DmMessage.account_id == account_id,
                            DmMessage.conv_id == item["conv_id"],
                            DmMessage.msg_id == msg_id,
                        )).first()
                        is_new = row is None
                        if row is None:
                            row = DmMessage(
                                platform="xhs", account_id=account_id,
                                conv_id=str(item["conv_id"]), msg_id=msg_id)
                        row.direction = str(msg.get("direction") or "in")
                        row.msg_type = str(msg.get("msg_type") or "unknown")
                        row.text = str(msg.get("text") or "")
                        row.create_time = int(msg.get("create_time") or 0)
                        row.raw_json = json.dumps({
                            "store_id": int(msg.get("store_id") or 0),
                            "sender_uid": msg.get("sender_uid") or "",
                            "receiver_uid": msg.get("receiver_uid") or "",
                            "group_chat": bool(msg.get("group_chat")),
                            "content": msg.get("content"),
                        }, ensure_ascii=False, separators=(",", ":"))
                        store_id = int(msg.get("store_id") or 0)
                        fresh = (is_new and store_id > old_max and row.direction == "in"
                                 and not bool(msg.get("group_chat")))
                        if fresh:
                            incoming_ids.append(msg_id)
                            events.append({
                                "type": "message", "account_id": account_id,
                                "conv_id": str(item["conv_id"]),
                                "text": row.text, "direction": "in",
                                "create_time": row.create_time,
                                "peer_uid": str(item.get("peer_uid") or ""),
                                "new_conversation": old_max == 0,
                            })
                        session.add(row)
                        if is_new:
                            message_count += 1
                    session.commit()
                for msg_id in incoming_ids:
                    outcome = self._evaluate_message(
                        account_id, str(item["conv_id"]), msg_id)
                    queued += int(outcome == "queued")
                    drafts += int(outcome == "draft")
                    if outcome in {"queued", "draft"}:
                        events.append({"type": "auto_reply", "account_id": account_id,
                                       "conv_id": item["conv_id"], "state": outcome})
                with get_session() as session:
                    stored_conv = session.exec(select(DmConversation).where(
                        DmConversation.account_id == account_id,
                        DmConversation.conv_id == str(item["conv_id"]),
                    )).first()
                    if stored_conv:
                        stored_conv.raw_json = str(item.get("raw_json") or "")
                        stored_conv.fetched_at = datetime.utcnow()
                        session.add(stored_conv); session.commit()

        finished = datetime.utcnow()
        with get_session() as session:
            row = session.get(DmMonitorState, monitor.id)
            if row:
                if not row.baseline_initialized:
                    row.baseline_initialized = True
                    row.baseline_at = finished
                row.last_poll_at = finished
                if trigger == "push":
                    row.last_push_at = finished
                row.last_error = ""
                row.updated_at = finished
                session.add(row); session.commit()
        self._last_poll_monotonic[account_id] = time.monotonic()
        return {"ok": True, "error": "", "conversations": len(convs),
                "messages": message_count, "queued": queued, "drafts": drafts,
                "events": events, "trigger": trigger,
                "baseline_initialized": True,
                "realtime": self.realtime_status(account_id)}

    def _evaluate_message(self, account_id: int, conv_id: str, msg_id: str) -> str:
        if not bool(self.cfg.engine.xhs_dm_auto_reply_enabled):
            return "disabled"
        now = datetime.utcnow()
        with get_session() as session:
            msg = session.exec(select(DmMessage).where(
                DmMessage.account_id == account_id,
                DmMessage.conv_id == conv_id,
                DmMessage.msg_id == msg_id,
            )).first()
            conv = session.exec(select(DmConversation).where(
                DmConversation.account_id == account_id,
                DmConversation.conv_id == conv_id,
            )).first()
            if not msg or not conv or msg.auto_reply_state:
                return "duplicate"

            # If a human/outgoing message is newer, the conversation is already handled.
            latest_out = session.exec(select(DmMessage).where(
                DmMessage.account_id == account_id,
                DmMessage.conv_id == conv_id,
                DmMessage.direction == "out",
            ).order_by(DmMessage.create_time.desc())).first()
            if latest_out and latest_out.create_time >= msg.create_time:
                msg.auto_reply_state = "already_replied"
                msg.auto_reply_processed_at = now
                session.add(msg); session.commit()
                return "already_replied"

            existing = session.exec(select(AccountActionTask).where(
                AccountActionTask.account_id == account_id,
                AccountActionTask.conv_id == conv_id,
                AccountActionTask.source_msg_id == msg_id,
            )).first()
            if existing:
                msg.auto_reply_state = "draft" if existing.status == "draft" else "queued"
                msg.auto_reply_task_id = existing.id
                session.add(msg); session.commit()
                return msg.auto_reply_state

            rules = session.exec(select(DmAutoReplyRule).where(
                DmAutoReplyRule.account_id == account_id,
                DmAutoReplyRule.platform == "xhs",
                DmAutoReplyRule.enabled == True,  # noqa: E712
            ).order_by(DmAutoReplyRule.id.asc())).all()
            matched: DmAutoReplyRule | None = None
            for rule in rules:
                age = max(0, int(time.time()) - int(msg.create_time or 0))
                if msg.create_time and age > max(60, int(rule.max_message_age_seconds)):
                    continue
                if rule_matches(rule, msg.text):
                    matched = rule
                    break
            if matched is None:
                msg.auto_reply_state = "no_match"
                msg.auto_reply_processed_at = now
                session.add(msg); session.commit()
                return "no_match"

            # One outstanding automated response per conversation.
            outstanding = session.exec(select(AccountActionTask).where(
                AccountActionTask.account_id == account_id,
                AccountActionTask.conv_id == conv_id,
                AccountActionTask.source_rule_id != None,  # noqa: E711
                AccountActionTask.status.in_(["draft", "pending", "doing"]),
            )).first()
            cutoff = now - timedelta(seconds=max(60, int(matched.cooldown_seconds)))
            recent = session.exec(select(AccountActionTask).where(
                AccountActionTask.account_id == account_id,
                AccountActionTask.conv_id == conv_id,
                AccountActionTask.source_rule_id == matched.id,
                AccountActionTask.created_at >= cutoff,
                AccountActionTask.status.in_(["draft", "pending", "doing", "done"]),
            )).first()
            if outstanding or recent:
                msg.auto_reply_state = "cooldown"
                msg.auto_reply_rule_id = matched.id
                msg.auto_reply_processed_at = now
                session.add(msg); session.commit()
                return "cooldown"

            content = render_reply(
                matched, nickname=conv.peer_nickname, message=msg.text)
            if not content:
                msg.auto_reply_state = "empty_template"
                msg.auto_reply_rule_id = matched.id
                msg.auto_reply_processed_at = now
                session.add(msg); session.commit()
                return "empty_template"
            low = max(15, int(matched.min_delay_seconds))
            high = max(low, int(matched.max_delay_seconds))
            task = AccountActionTask(
                platform="xhs", account_id=account_id, action="send_dm",
                target_uid=conv.peer_uid or conv.conv_id,
                target_sec_uid=conv.peer_sec_uid, target_nick=conv.peer_nickname,
                conv_id=conv.conv_id, content=content,
                source_msg_id=msg.msg_id, source_rule_id=matched.id,
                scheduled_at=now + timedelta(seconds=random.randint(low, high)),
                status="draft" if matched.review_before_send else "pending",
                min_gap_seconds=max(60, int(self.cfg.risk_control.dm_min_gap_seconds)),
            )
            session.add(task); session.flush()
            msg.auto_reply_state = "draft" if matched.review_before_send else "queued"
            msg.auto_reply_rule_id = matched.id
            msg.auto_reply_task_id = task.id
            msg.auto_reply_processed_at = now
            session.add(msg); session.commit()
            return msg.auto_reply_state
