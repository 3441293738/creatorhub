# -*- coding: utf-8 -*-
"""抖音私信实时接收管理器。

每个订阅中的账号维持一条 frontier-im WS 长连接:收到新消息即入库(DmMessage/更新
DmConversation)并推给该账号的所有 SSE 订阅者。连接生命周期跟随订阅——有前端在看
就连,最后一个订阅断开就停(避免常驻空跑;要"离线也收/做通知"再改成常驻)。
"""
import asyncio
import json
from datetime import datetime
from typing import Dict, Set

from sqlmodel import select

from ..browser.account_hub import _douyin_cookie_str
from ..browser.douyin_im_ws import receive_messages
from ..db import get_session
from ..models import DmConversation, DmMessage, DouyinAccount

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
        if not st["task"] or st["task"].done():
            st["task"] = asyncio.create_task(self._run(account_id))
        return q

    def unsubscribe(self, account_id: int, q: asyncio.Queue):
        st = self._accts.get(account_id)
        if not st:
            return
        st["queues"].discard(q)
        if not st["queues"] and st["task"]:
            st["task"].cancel()
            st["task"] = None

    async def stop_all(self):
        for st in self._accts.values():
            if st.get("task"):
                st["task"].cancel()
        self._accts.clear()

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
            self._handle(account_id, m)

        def should_stop():
            st = self._accts.get(account_id)
            return not st or not st["queues"]

        await receive_messages(cookie, uid, on_msg, self_uid=uid,
                               should_stop=should_stop)

    def _handle(self, account_id: int, m: dict):
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
                    platform="douyin", account_id=account_id, conv_id=conv_id,
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
