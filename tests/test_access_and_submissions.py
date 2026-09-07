"""Offline regressions for a local-only workbench and durable task creation."""
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException, Request
from sqlmodel import select

import app.db as db
import app.main as main
from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import (AccountActionTask, CommentRule, ContentRecord,
                        DmAutoReplyRule, DouyinAccount, KeywordCollectionJob,
                        NotificationChannel, PublishTask, TaskSubmission)
from app.notification_config import MASK, redact_detail, redact_config
from app.submissions import submit_once
from test_project_optimizations import local_project, store


def client(*, peer="127.0.0.1", base="http://127.0.0.1"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(
        app=main.app, client=(peer, 1234)), base_url=base)


def test_loopback_defaults_and_no_login_system(local_project, monkeypatch):
    import creatorhub
    monkeypatch.setattr(creatorhub, "CONFIG_FILE", local_project.root / "missing.yaml")
    assert Config().server.host == "127.0.0.1"
    assert creatorhub.read_server_defaults() == ("127.0.0.1", 8000)
    async def scenario():
        async with client() as http:
            for path in ("/", "/api/settings", "/api/notifications", "/health"):
                response = await http.get(path)
                assert response.status_code == 200
                assert "set-cookie" not in response.headers
            assert (await http.get("/api/auth/session")).status_code == 404
            assert (await http.get("/static/submissions.js")).status_code == 200
            assert "access-dialog" not in (await http.get("/")).text
    asyncio.run(scenario())


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/accounts"), ("GET", "/api/notifications"),
    ("GET", "/api/settings"), ("GET", "/api/proxies/options"),
    ("POST", "/api/publish"), ("POST", "/api/publish/upload"),
    ("POST", "/api/account-actions"), ("POST", "/api/accounts/1/open-browser"),
    ("DELETE", "/api/accounts/1"), ("GET", "/api/dm/stream?account_id=1"),
    ("GET", "/api/contents/1/local-media"), ("HEAD", "/api/contents/1/local-media"),
    ("GET", "/api/reports/contents.xlsx"), ("GET", "/openapi.json"),
    ("GET", "/docs"), ("POST", "/api/not-yet-a-route"),
])
def test_remote_requests_are_blocked_before_dispatch(local_project, method, path):
    async def scenario():
        async with client(peer="192.0.2.8") as http:
            response = await http.request(method, path)
            assert response.status_code == 403
            assert response.headers["cache-control"] == "no-store"
    asyncio.run(scenario())
    with db.get_session() as session:
        assert not session.exec(select(TaskSubmission)).all()


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"}, {"Origin": "null"},
    {"Origin": "http://127.0.0.1:9000"}, {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"}, {"Host": "rebind.example"},
])
def test_cross_origin_or_rebound_host_cannot_call_local_api(local_project, headers):
    async def scenario():
        async with client() as http:
            response = await http.get("/api/notifications", headers=headers)
            assert response.status_code == 403
            response = await http.post("/api/accounts/1/open-browser", headers=headers)
            assert response.status_code == 403
    asyncio.run(scenario())


def test_local_origin_and_untrusted_forwarding(local_project):
    local_project.cfg.server.host = "0.0.0.0"
    async def scenario():
        async with client() as http:
            assert (await http.get("/api/settings")).status_code == 200
            response = await http.put("/api/settings", json={}, headers={"Origin": "http://127.0.0.1:80"})
            assert response.status_code == 200
            assert response.headers["x-frame-options"] == "DENY"
        async with client(peer="192.0.2.8") as http:
            result = await http.get("/api/settings", headers={"X-Forwarded-For": "127.0.0.1"})
            assert result.status_code == 403
        async with client(base="http://remote.example.test") as http:
            assert (await http.get("/api/settings")).status_code == 403
        async with client(peer="::1", base="http://[::1]") as http:
            assert (await http.get("/api/settings")).status_code == 200
    asyncio.run(scenario())


def test_notification_secrets_are_write_only_and_mask_preserves_latest(local_project, monkeypatch):
    sender = AsyncMock(return_value=(False, "request secret-new failed"))
    monkeypatch.setattr(main, "send_one", sender)
    async def scenario():
        async with client() as http:
            response = await http.post("/api/notifications", json={"name": "fixture", "type": "telegram",
                "config": {"bot_token": "secret-old", "chat_id": "42", "custom": {"password": "nested-secret"}}})
            assert response.status_code == 200
            row = response.json()
            assert row["config"] == {"bot_token": MASK, "chat_id": "42", "custom": MASK}
            assert "secret-old" not in response.text and "nested-secret" not in response.text
            path = f"/api/notifications/{row['id']}"
            await http.put(path, json={"config": {"bot_token": "secret-new"}})
            response = await http.put(path, json={"name": "edited", "config": row["config"]})
            assert response.status_code == 200
            with db.get_session() as session:
                config = json.loads(session.get(NotificationChannel, row["id"]).config)
                assert config["bot_token"] == "secret-new" and config["custom"]["password"] == "nested-secret"
            assert "secret-new" not in (await http.get("/api/notifications")).text
            result = await http.post(path + "/test")
            assert "secret-new" not in result.text and MASK in result.text
            assert sender.await_args.args[1]["bot_token"] == "secret-new"
            await http.put(path, json={"config": {"bot_token": ""}})
            with db.get_session() as session:
                assert json.loads(session.get(NotificationChannel, row["id"]).config)["bot_token"] == ""
            assert (await http.post("/api/notifications", json={"type": "bark", "config": {"key": MASK}})).status_code == 422
    asyncio.run(scenario())
    assert "SECRET" not in redact_detail("dingtalk", {"webhook": "https://example.test/send?access_token=SECRET"}, "failed with SECRET")
    assert redact_config("bark", {"server": {"password": "SECRET"}}) == {"server": MASK}


def test_publish_receipt_survives_restart_and_does_not_revalidate_old_files(local_project):
    account = store(DouyinAccount(platform="douyin", storage_state="{}"))
    media = local_project.root / "fixture.jpg"
    media.write_bytes(b"local fixture")
    body = {"account_id": account, "media_paths": [str(media)], "title": "fixture"}
    headers = {"Idempotency-Key": "fixture-publish-0001"}
    async def scenario():
        async with client() as http:
            first = await http.post("/api/publish", json=body, headers=headers)
            assert first.status_code == 200
            db._engine.dispose(); db.init_db(str(local_project.path))
            media.unlink()
            replay = await http.post("/api/publish", json=body, headers=headers)
            assert replay.status_code == 200 and replay.json()["replayed"]
            assert replay.json()["id"] == first.json()["id"]
            mismatch = await http.post("/api/publish", json={**body, "title": "different"}, headers=headers)
            assert mismatch.status_code == 409
            with db.get_session() as session:
                assert len(session.exec(select(PublishTask)).all()) == 1
                assert len(session.exec(select(TaskSubmission)).all()) == 1
    asyncio.run(scenario())


def test_validation_rollback_and_intentional_new_submission(local_project):
    account = store(DouyinAccount(platform="douyin", storage_state="{}"))
    media = local_project.root / "fixture.jpg"
    body = {"account_id": account, "media_paths": [str(media)]}
    async def scenario():
        async with client() as http:
            headers = {"Idempotency-Key": "fixture-publish-0001"}
            assert (await http.post("/api/publish", json=body, headers=headers)).status_code == 400
            with db.get_session() as session:
                assert not session.exec(select(TaskSubmission)).all()
            media.write_bytes(b"local fixture")
            a = await http.post("/api/publish", json=body, headers=headers)
            b = await http.post("/api/publish", json=body, headers={"Idempotency-Key": "fixture-publish-0002"})
            assert a.status_code == b.status_code == 200 and a.json()["id"] != b.json()["id"]
            assert (await http.post("/api/publish", json=body, headers={"Idempotency-Key": "bad"})).status_code == 422
            # Deleted IDs must not resolve a receipt to a different later task.
            with db.get_session() as session:
                original = session.get(PublishTask, a.json()["id"])
                session.delete(original); session.commit()
                session.add(PublishTask(id=a.json()["id"])); session.commit()
            assert (await http.post("/api/publish", json=body, headers=headers)).status_code == 410
    asyncio.run(scenario())


def test_receipt_and_task_are_atomic_under_real_sqlite_concurrency(local_project):
    barrier = threading.Barrier(4)
    request = Request({"type": "http", "headers": [(b"idempotency-key", b"fixture-parallel-key")]})
    def worker():
        barrier.wait()
        with db.get_session() as session:
            def create():
                row = PublishTask(title="one logical submission")
                session.add(row); session.flush()
                return {"id": row.id}
            return submit_once(session, request=request, scope="publish", body={"title": "fixture"}, create=create)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: worker(), range(4)))
    assert sum(created for _, created in results) == 1
    assert len({payload["id"] for payload, _ in results}) == 1
    with db.get_session() as session:
        assert len(session.exec(select(PublishTask)).all()) == 1
        assert len(session.exec(select(TaskSubmission)).all()) == 1


def test_callback_failure_rolls_back_receipt_and_task(local_project):
    request = Request({"type": "http", "headers": [(b"idempotency-key", b"fixture-rollback-key")]})
    with pytest.raises(RuntimeError), db.get_session() as session:
        def create():
            session.add(PublishTask(title="not committed")); session.flush()
            raise RuntimeError("crash before commit")
        submit_once(session, request=request, scope="publish", body={}, create=create)
    with db.get_session() as session:
        assert not session.exec(select(PublishTask)).all()
        assert not session.exec(select(TaskSubmission)).all()


def test_failed_run_now_keeps_task_and_never_dispatches_replay(local_project, monkeypatch):
    account = store(DouyinAccount(platform="douyin"))
    execute = AsyncMock(side_effect=HTTPException(503, "engine not ready"))
    monkeypatch.setattr(main, "_exec_action", execute)
    async def scenario():
        async with client() as http:
            body = {"account_id": account, "action": "send_dm", "target_uid": "peer", "content": "hello", "run_now": True}
            headers = {"Idempotency-Key": "fixture-action-0001"}
            first = await http.post("/api/account-actions", json=body, headers=headers)
            assert first.status_code == 200 and first.json()["status"] == "pending" and not first.json()["ran"]
            task_id = first.json()["id"]
            with db.get_session() as session:
                task = session.get(AccountActionTask, task_id)
                task.status = "uncertain"; session.add(task); session.commit()
            replay = await http.post("/api/account-actions", json=body, headers=headers)
            assert replay.status_code == 200 and replay.json()["id"] == task_id
            assert replay.json()["status"] == "uncertain" and replay.json()["replayed"]
            execute.assert_awaited_once()
    asyncio.run(scenario())


def test_concurrent_run_now_reuses_task_while_execution_is_inflight(local_project, monkeypatch):
    account = store(DouyinAccount(platform="douyin"))
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def execute(task_id):
            with db.get_session() as session:
                task = session.get(AccountActionTask, task_id)
                task.status = "doing"; session.add(task); session.commit()
            started.set(); await release.wait()
            with db.get_session() as session:
                task = session.get(AccountActionTask, task_id)
                task.status = "done"; session.add(task); session.commit()
            return True, ""
        mock = AsyncMock(side_effect=execute)
        monkeypatch.setattr(main, "_exec_action", mock)
        async with client() as http:
            body = {"account_id": account, "action": "follow", "target_uid": "peer", "run_now": True}
            headers = {"Idempotency-Key": "fixture-running-key"}
            first = asyncio.create_task(http.post("/api/account-actions", json=body, headers=headers))
            await started.wait()
            replay = await http.post("/api/account-actions", json=body, headers=headers)
            assert replay.status_code == 200 and replay.json()["status"] == "doing"
            release.set()
            result = await first
            assert result.json()["ran"] and result.json()["id"] == replay.json()["id"]
            mock.assert_awaited_once()
    asyncio.run(scenario())


def test_repost_rule_and_collection_creation_use_durable_receipts(local_project, monkeypatch):
    account = store(DouyinAccount(platform="douyin", storage_state="{}"))
    xhs = store(DouyinAccount(platform="xhs", storage_state="{}"))
    content = store(ContentRecord(target_id=0, aweme_id="fixture", download_status="done"))
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(engine, "_content_files", lambda _: [str(local_project.root / "fixture.jpg")])
    enqueue = Mock()
    monkeypatch.setattr(engine, "enqueue_collection_job", enqueue)
    resolve = AsyncMock(return_value=("self", "", "", "", ""))
    monkeypatch.setattr(main, "_resolve_rule_target", resolve)
    async def scenario():
        async with client() as http:
            for path, body, model in [
                (f"/api/contents/{content}/repost-douyin", {"account_id": account}, PublishTask),
                ("/api/comment-rules", {"account_id": account, "templates": ["hello"], "target_kind": "self"}, CommentRule),
                ("/api/dm/auto-reply-rules", {"account_id": xhs, "keywords": ["hello"], "reply_templates": ["hello"]}, DmAutoReplyRule),
                ("/api/collections", {"account_id": account, "keywords": ["fixture"]}, KeywordCollectionJob),
            ]:
                headers = {"Idempotency-Key": "fixture-shared-key"}
                first = await http.post(path, json=body, headers=headers)
                assert first.status_code == 200, first.text
                second = await http.post(path, json=body, headers=headers)
                assert second.status_code == 200 and second.json()["replayed"]
                with db.get_session() as session:
                    assert len(session.exec(select(model)).all()) == 1
            resolve.assert_awaited_once()
            enqueue.assert_called_once()
    asyncio.run(scenario())
