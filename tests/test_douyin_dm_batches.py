import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from app import db, main, douyin_dm_batches as dm
from app.models import AccountActionTask, DmConversation, DouyinAccount
from test_project_optimizations import local_project, store
from test_access_and_submissions import client


@pytest.fixture
def active(local_project, monkeypatch):
    monkeypatch.setattr(main.licensing, "is_active", lambda: True)
    return store(DouyinAccount(nickname="sender"))


def test_preview_deduplicates_contacts_links_and_reports_invalid(active, monkeypatch):
    contact = store(DmConversation(account_id=active, conv_id="conv", peer_uid="1", peer_nickname="A"))
    async def resolve(account, link):
        if link == "invalid": raise ValueError("无效链接")
        return dm.Recipient(target_uid="1", target_sec_uid="sec", target_nick="A")
    monkeypatch.setattr(dm, "resolve_profile", resolve)
    async def scenario():
        async with client() as http:
            response = await http.post("/api/douyin/dm/preview", json={"account_id": active,
                "links": ["profile", "invalid"], "contact_ids": [contact, 9999]})
            assert response.status_code == 200, response.text
            result = response.json()
            assert len(result["recipients"]) == 1 and result["duplicates"] == 1
            assert len(result["invalid"]) == 2
    asyncio.run(scenario())


def test_batch_replay_cancel_partial_and_restart(active):
    async def scenario():
        async with client() as http:
            body = {"account_id": active, "content": "hello", "recipients": [
                {"target_uid": str(i)} for i in [1, 1, 2, 3, 4, 5]]}
            headers = {"Idempotency-Key": "fixture-batch-0001"}
            response = await http.post("/api/douyin/dm/batches", json=body, headers=headers)
            assert response.status_code == 200, response.text
            batch = response.json()
            assert batch["total"] == 5 and batch["counts"]["pending"] == 5
            with db.get_session() as session:
                for task_id, status in zip(batch["task_ids"], ["done", "failed", "uncertain", "doing"]):
                    task = session.get(AccountActionTask, task_id)
                    task.status = status; session.add(task)
                session.commit()
            replay = await http.post("/api/douyin/dm/batches", json=body, headers=headers)
            assert replay.json()["replayed"] and replay.json()["task_ids"] == batch["task_ids"]
            assert replay.json()["counts"]["done"] == 1
            # Dispose and reopen database connections as on restart; durable receipt remains.
            db._engine.dispose()
            restored = await http.get(f"/api/douyin/dm/batches/{batch['batch_id']}")
            assert restored.json()["counts"]["uncertain"] == 1
            canceled = await http.post(f"/api/douyin/dm/batches/{batch['batch_id']}/cancel")
            assert canceled.json()["counts"] == dict(pending=0, doing=1, done=1, failed=1, uncertain=1, canceled=1)
            listed = await http.get(f"/api/douyin/dm/batches?account_id={active}")
            assert len(listed.json()) == 1
            body["content"] = "changed"
            assert (await http.post("/api/douyin/dm/batches", json=body, headers=headers)).status_code == 409
            with db.get_session() as session:
                assert len(session.exec(select(AccountActionTask)).all()) == 5
    asyncio.run(scenario())


def test_empty_missing_key_and_license_rejected(active, monkeypatch):
    async def scenario():
        async with client() as http:
            body = {"account_id": active, "content": "  ", "recipients": [{"target_uid": "1"}]}
            headers = {"Idempotency-Key": "fixture-batch-0002"}
            assert (await http.post("/api/douyin/dm/batches", json=body, headers=headers)).status_code == 422
            body["content"] = "hello"
            assert (await http.post("/api/douyin/dm/batches", json=body)).status_code == 422
            monkeypatch.setattr(main.licensing, "is_active", lambda: False)
            response = await http.post("/api/douyin/dm/batches", json=body, headers=headers)
            assert response.status_code == 403 and response.json()["code"] == "license_required"
            with db.get_session() as session:
                assert not session.exec(select(AccountActionTask)).all()
    asyncio.run(scenario())


def test_invalid_url_never_calls_platform(active):
    with db.get_session() as session:
        account = session.get(DouyinAccount, active)
        for link in ["https://evil.example/user/abc", "", "https://www.douyin.com/video/123"]:
            with pytest.raises(ValueError, match="主页链接"):
                asyncio.run(dm.resolve_profile(account, link))


def test_identity_bridge_merges_both_existing_groups():
    rows = dm.deduplicate([dm.Recipient(target_uid="1"),
        dm.Recipient(target_sec_uid="s", conv_id="c"),
        dm.Recipient(target_uid="1", target_sec_uid="s")])
    assert len(rows) == 1
    assert (rows[0].target_uid, rows[0].target_sec_uid, rows[0].conv_id) == ("1", "s", "c")


def test_invalid_account_and_batch_rollback(active):
    async def scenario():
        async with client() as http:
            body = {"account_id": active, "content": "hello", "recipients": [
                {"target_uid": "1"}, {"target_uid": "2", "conv_id": "foreign"}]}
            headers = {"Idempotency-Key": "fixture-rollback-0001"}
            assert (await http.post("/api/douyin/dm/batches", json=body, headers=headers)).status_code == 422
            with db.get_session() as session:
                assert not session.exec(select(AccountActionTask)).all()
                account = session.get(DouyinAccount, active)
                account.status = "invalid"; session.add(account); session.commit()
            body["recipients"] = [{"target_uid": "1"}]
            assert (await http.post("/api/douyin/dm/batches", json=body, headers=headers)).status_code == 400
    asyncio.run(scenario())


def test_profile_resolution_uses_selected_account_read_guard(active, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    seen = {}
    class FakeClient:
        def __init__(self, cookie, ua, **kwargs):
            seen["cookie"] = cookie
        @asynccontextmanager
        async def session_scope(self):
            yield self
        async def resolve_user_identifier(self, value):
            seen["identifier"] = value
            return {"uid": "123", "sec_uid": "MS4wLjABabc", "nickname": "接收人"}, ""
    async def guarded(account_id, kind, key, operation, **kwargs):
        seen["account_id"] = account_id
        return await operation()
    monkeypatch.setattr(main, "DouyinClient", FakeClient)
    monkeypatch.setattr(main, "douyin_cookie_from_state", lambda state: "isolated-cookie")
    monkeypatch.setattr(main, "engine", SimpleNamespace(guarded_read_pair=guarded))
    result = asyncio.run(dm.preview(dm.PreviewIn(account_id=active,
        links=["https://www.douyin.com/user/MS4wLjABabc"])))
    assert result["recipients"][0]["target_uid"] == "123"
    assert seen == {"cookie": "isolated-cookie", "identifier": "MS4wLjABabc", "account_id": active}


def test_restart_recovers_batch_doing_without_replaying_terminal_tasks(active, local_project):
    from app.engine.monitor import MonitorEngine
    from test_write_gates import _BrowserStub
    async def scenario():
        async with client() as http:
            body = {"account_id": active, "content": "restart", "recipients": [
                {"target_uid": str(i)} for i in range(1, 5)]}
            headers = {"Idempotency-Key": "fixture-restart-batch-01"}
            batch = (await http.post('/api/douyin/dm/batches', json=body, headers=headers)).json()
            with db.get_session() as session:
                for ident, status in zip(batch['task_ids'], ['done', 'doing', 'failed', 'pending']):
                    task=session.get(AccountActionTask,ident); task.status=status; session.add(task)
                session.commit()
            db._engine.dispose()
            db.init_db(str(local_project.path))
            engine=MonitorEngine(local_project.cfg,_BrowserStub())
            assert engine.recover_interrupted_tasks()==1
            for ident in batch['task_ids'][:3]:
                result=await engine.execute_action_task(ident)
                assert not result['ok']
            replay=(await http.post('/api/douyin/dm/batches',json=body,headers=headers)).json()
            assert replay['replayed'] and replay['task_ids']==batch['task_ids']
            assert replay['counts']==dict(pending=1,doing=0,done=1,failed=1,uncertain=1,canceled=0)
    asyncio.run(scenario())


def test_api_batch_executes_new_conversations_and_continues_after_recipient_rejection(active, local_project, monkeypatch):
    import json
    from contextlib import asynccontextmanager
    from app.engine.monitor import MonitorEngine
    from test_write_gates import _BrowserStub
    calls=[]
    class Platform:
        def __init__(self,*args,**kwargs): pass
        @asynccontextmanager
        async def session_scope(self): yield self
        async def create_dm_conversation(self, peer, sender, **kwargs):
            calls.append(('create',peer))
            return dict(conv_id='conv-'+peer,conv_short_id='42',ticket='fixture'),''
        async def send_dm(self,conv,short,ticket,content):
            calls.append(('send',conv))
            return (False,'recipient does not accept messages') if conv=='conv-101' else (True,'')
    monkeypatch.setattr('app.engine.monitor.DouyinClient',Platform)
    monkeypatch.setattr('app.engine.monitor.send_dm',AsyncMock(side_effect=AssertionError('no fallback')))
    with db.get_session() as session:
        account=session.get(DouyinAccount,active); account.uid='999'
        account.storage_state=json.dumps({'cookies':[{'domain':'.douyin.com','name':'sid_tt','value':'offline'}]})
        session.add(account); session.commit()
    local_project.cfg.engine.douyin_write_mode='api'
    local_project.cfg.engine.quiet_hours_enabled=False
    engine=MonitorEngine(local_project.cfg,_BrowserStub())
    async def scenario():
        async with client() as http:
            body=dict(account_id=active,content='offline',recipients=[dict(target_uid='101',target_sec_uid='sec-a'),dict(target_uid='102',target_sec_uid='sec-b')])
            headers={'Idempotency-Key':'fixture-execute-batch-01'}
            response=await http.post('/api/douyin/dm/batches',json=body,headers=headers)
            assert response.status_code==200,response.text
            batch=response.json()
            first=await engine.execute_action_task(batch['task_ids'][0]); assert not first['ok']
            # Advance the risk clock past ordinary per-operation pacing, without bypassing guards.
            from datetime import datetime, timedelta
            monkeypatch.setattr('app.risk._utcnow', lambda: datetime.utcnow()+timedelta(hours=1))
            second=await engine.execute_action_task(batch['task_ids'][1]); assert second['ok'],second
            replay=(await http.post('/api/douyin/dm/batches',json=body,headers=headers)).json()
            assert replay['counts']['failed']==1 and replay['counts']['done']==1
            assert replay['task_ids']==batch['task_ids']
            assert not (await engine.execute_action_task(batch['task_ids'][1]))['ok']
    asyncio.run(scenario())
    assert calls==[('create','101'),('send','conv-101'),('create','102'),('send','conv-102')]
