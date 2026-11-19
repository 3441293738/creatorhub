# -*- coding: utf-8 -*-
"""抖音与哔哩哔哩私信实时接收管理器。

每个订阅中的账号维持一条 WS 长连接:
  - 抖音: frontier-im WS 长连接，收到新消息即入库并推给 SSE 订阅者；
  - 哔哩哔哩: broadcast.chat.bilibili.com:7826/sub (8 类型 Typed Protobuf)，
    收到 WatchNotify 时唤醒 HTTP new_sessions + fetch_session_msgs 增量同步。
连接生命周期跟随订阅——有前端在看就连，最后一个订阅断开就停。
"""
import asyncio
import json
import time
from datetime import datetime
from typing import Dict

from sqlmodel import select

from ..browser.account_hub import _bili_cookie_str, _douyin_cookie_str
from ..browser.douyin_im_ws import receive_messages
from ..db import get_session
from ..models import DmConversation, DmMessage, DouyinAccount
from ..platforms.bilibili import BilibiliClient, cookie_str_from_state, receive_bili_broadcast

# 非聊天正文的消息类型(已读回执等),不入库/不推
_SKIP_MSG_TYPES = {50001, 50002}


class ImReceiverManager:
    def __init__(self, browser):
        self.browser = browser
        self._accts: Dict[int, dict] = {}   # account_id -> {task, queues:set, uid}

    async def subscribe(self, account_id: int) -> asyncio.Queue:
        st = self._accts.get(account_id)
        if not st:
            st = {"task": None, "queues": set(), "uid": ""}
            self._accts[account_id] = st
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        st["queues"].add(q)
        with get_session() as session:
            account = session.get(DouyinAccount, account_id)
            platform = str(account.platform if account else "")
        # XHS owns one account-wide socket in the resident first-party page.
        # SSE subscribers only consume events; they never create another socket.
        if platform == "douyin" and (not st["task"] or st["task"].done()):
            st["task"] = asyncio.create_task(self._run(account_id))
        elif platform == "bilibili" and (not st["task"] or st["task"].done()):
            st["task"] = asyncio.create_task(self._run_bilibili(account_id))
        return q

    def unsubscribe(self, account_id: int, q: asyncio.Queue):
        st = self._accts.get(account_id)
        if not st:
            return
        st["queues"].discard(q)
        if not st["queues"] and st["task"]:
            st["task"].cancel()
            st["task"] = None

    async def stop_account(self, account_id: int):
        state = self._accts.pop(account_id, None)
        task = state.get("task") if state else None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def stop_all(self):
        for account_id in list(self._accts):
            await self.stop_account(account_id)

    def publish(self, account_id: int, event: dict) -> None:
        """Publish an externally received account DM event to SSE clients."""
        st = self._accts.get(account_id)
        if not st:
            return
        for queue in list(st["queues"]):
            try:
                queue.put_nowait(event)
            except Exception:
                pass

    async def _run(self, account_id: int):
        # 等 uid(允许"先开面板后同步"):最多轮询 ~30s 读 DB,同步存下 uid 即自愈
        uid, identity = "", None
        for _ in range(15):
            with get_session() as s:
                acc = s.get(DouyinAccount, account_id)
                if not acc or acc.platform != "douyin":
                    print(f"[im-ws] _run account={account_id} 非抖音/不存在,跳过")
                    return
                uid = acc.uid
                identity = self.browser.identity_for(acc)
            if uid:
                break
            st = self._accts.get(account_id)
            if not st or not st["queues"]:      # 订阅者都走了就别等了
                return
            await asyncio.sleep(2)
        print(f"[im-ws] _run account={account_id} uid={uid!r}")
        if not uid:
            print(f"[im-ws] account {account_id} 无 uid,点一次「同步」存下 uid 即自动接上")
            return
        try:
            cookie = await _douyin_cookie_str(self.browser, identity)
        except Exception as e:
            print(f"[im-ws] account {account_id} 取 cookie 失败: {e!r}")
            return

        def on_msg(m):
            self._handle(account_id, m, platform="douyin")

        def should_stop():
            st = self._accts.get(account_id)
            return not st or not st["queues"]

        await receive_messages(cookie, uid, on_msg, self_uid=uid,
                               should_stop=should_stop)

    async def _run_bilibili(self, account_id: int):
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            if not acc or acc.platform != "bilibili":
                return
            identity = self.browser.identity_for(acc)
            ua = str(acc.ua or "")
            proxy = str(acc.proxy or "")
            self_uid = str(acc.uid or acc.sec_uid or "")
            fallback_cookie = cookie_str_from_state(acc.storage_state or acc.cookie or "")
        try:
            cookie = await _bili_cookie_str(self.browser, identity)
        except Exception:
            cookie = fallback_cookie
        if not cookie:
            print(f"[bili-im-ws] account {account_id} 无有效 Cookie，跳过实时私信连接")
            return

        begin_ts = int(time.time() * 1_000_000)

        def should_stop():
            st = self._accts.get(account_id)
            return not st or not st["queues"]

        async def on_watch_notify():
            nonlocal begin_ts, self_uid
            client = BilibiliClient(cookie, ua, proxy=proxy)
            async with client.session_scope():
                if not self_uid and client.self_mid:
                    self_uid = client.self_mid
                sessions, max_ts, err = await client.fetch_new_dm_sessions(begin_ts)
                if err:
                    return
                if max_ts > begin_ts:
                    begin_ts = max_ts
                for cdict in sessions:
                    peer = str(cdict.get("peer_uid") or cdict.get("conv_id") or "")
                    if not peer:
                        continue
                    try:
                        with get_session() as s:
                            conv = s.exec(
                                select(DmConversation).where(
                                    DmConversation.account_id == account_id,
                                    DmConversation.conv_id == peer,
                                )
                            ).first()
                            if not conv:
                                conv = DmConversation(
                                    platform="bilibili",
                                    account_id=account_id,
                                    conv_id=peer,
                                    peer_uid=peer,
                                    peer_sec_uid=peer,
                                    peer_nickname=str(cdict.get("peer_nickname") or ""),
                                    peer_avatar=str(cdict.get("peer_avatar") or ""),
                                    last_text=str(cdict.get("last_text") or ""),
                                    last_time=int(cdict.get("last_time") or 0),
                                    unread_count=int(cdict.get("unread_count") or 0),
                                    conv_short_id=peer,
                                    raw_json=str(cdict.get("raw_json") or ""),
                                    fetched_at=datetime.utcnow(),
                                )
                                s.add(conv)
                                s.commit()
                    except Exception:
                        pass
                    hist, herr = await client.fetch_dm_messages(
                        peer, size=10, self_uid=self_uid
                    )
                    if herr:
                        continue
                    for msg in (hist.get("messages") or []):
                        self._handle(
                            account_id,
                            {
                                "conv_id": peer,
                                "server_msg_id": str(msg.get("server_msg_id") or ""),
                                "is_self": msg.get("direction") == "out",
                                "text": str(msg.get("text") or ""),
                                "msg_type": 1,
                                "create_time": int(msg.get("create_time") or 0),
                                "sender_uid": str(msg.get("sender_uid") or ""),
                                "peer_uid": peer,
                            },
                            platform="bilibili",
                        )

        await receive_bili_broadcast(
            cookie,
            ua,
            on_watch_notify,
            should_stop=should_stop,
            proxy=proxy,
        )

    def _handle(self, account_id: int, m: dict, *, platform: str = "douyin"):
        if m.get("msg_type") in _SKIP_MSG_TYPES or not m.get("text"):
            return
        conv_id = m.get("conv_id") or ""
        mid = m.get("server_msg_id") or ""
        direction = "out" if m.get("is_self") else "in"
        ts = int(m.get("create_time") or 0)
        try:
            with get_session() as s:
                if mid:
                    exists = s.exec(select(DmMessage).where(
                        DmMessage.account_id == account_id,
                        DmMessage.msg_id == mid)).first()
                    if exists:
                        return
                # 删掉该会话的 last:<conv> 占位(实时消息更权威)
                ph = s.exec(select(DmMessage).where(
                    DmMessage.account_id == account_id,
                    DmMessage.msg_id == "last:" + conv_id)).first()
                if ph:
                    s.delete(ph)
                card = m.get("card")
                s.add(DmMessage(
                    platform=platform, account_id=account_id, conv_id=conv_id,
                    msg_id=mid or f"ws:{ts}:{m.get('sender_uid')}",
                    direction=direction, msg_type=("video" if card else "text"),
                    text=m.get("text") or "", create_time=ts,
                    raw_json=json.dumps(card, ensure_ascii=False) if card else ""))
                conv = s.exec(select(DmConversation).where(
                    DmConversation.account_id == account_id,
                    DmConversation.conv_id == conv_id)).first()
                if conv:
                    conv.last_text = m.get("text") or conv.last_text
                    conv.last_time = ts or conv.last_time
                    if direction == "in":
                        conv.unread_count = (conv.unread_count or 0) + 1
                    s.add(conv)
                s.commit()
        except Exception as e:
            print(f"[im-ws] 入库失败: {e!r}")
        # 推给 SSE 订阅者
        st = self._accts.get(account_id)
        if st:
            evt = {"conv_id": conv_id, "text": m.get("text"),
                   "direction": direction, "create_time": ts, "card": m.get("card"),
                   "sender_uid": m.get("sender_uid"), "peer_uid": m.get("peer_uid")}
            for q in list(st["queues"]):
                try:
                    q.put_nowait(evt)
                except Exception:
                    pass
