"""Offline regressions for account retirement, scheduling and write outcomes."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx
from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel import select
from starlette.requests import Request

import app.db as db
import app.main as main
from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import (
    AccountActionTask, AccountIdReservation, CommentRecord, CommentRule,
    CommentTask, CommentWatch, ContentRecord, DanmakuWatch, DmAutoReplyRule,
    DouyinAccount, KeywordCollectionJob, MonitorTarget, PublishTask,
    RiskAdminAudit, RiskEvent,
)
from app.risk import OperationKind
from app.scheduling import parse_schedule, utc_iso


class BrowserStub:
    def __init__(self):
        self.locks = {}
        self.close_context = AsyncMock()

    def lock_for(self, key):
        return self.locks.setdefault(key, asyncio.Lock())


@pytest.fixture
def local_project(tmp_path, monkeypatch):
    previous_engine = db._engine
    db_path = tmp_path / "optimizations.db"
    db.init_db(str(db_path))
    cfg = Config()
    cfg.engine.profiles_dir = str(tmp_path / "profiles")
    cfg.engine.media_dir = str(tmp_path / "media")
    browser = BrowserStub()
    automation = SimpleNamespace(stop_account=AsyncMock())
    monkeypatch.setattr(main, "cfg", cfg)
    monkeypatch.setattr(main, "browser", browser)
    monkeypatch.setattr(main, "engine", SimpleNamespace(dm_automation=automation))
    monkeypatch.setattr(main, "im_receiver", SimpleNamespace(stop_account=AsyncMock()))
    monkeypatch.setattr(main, "login_tasks", {})
    monkeypatch.setattr(main, "open_browsers", {})
    monkeypatch.delenv("CREATORHUB_ADMIN_TOKEN", raising=False)
    yield SimpleNamespace(path=db_path, root=tmp_path, cfg=cfg, browser=browser)
    db._engine.dispose()
    db._engine = previous_engine


def store(row):
    with db.get_session() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def test_account_retirement_is_atomic_and_preserves_history(local_project):
    account_id = store(DouyinAccount(nickname="old"))
    task_ids = [(model, store(model(account_id=account_id, status=status)))
                for model in (PublishTask, CommentTask, AccountActionTask)
                for status in ("draft", "pending", "failed")]
    job_id = store(KeywordCollectionJob(account_id=account_id, status="partial"))
    enabled_ids = [(model, store(model(account_id=account_id, enabled=True)))
                   for model in (MonitorTarget, CommentWatch, DanmakuWatch,
                                 CommentRule, DmAutoReplyRule)]
    history = store(PublishTask(account_id=account_id, status="uncertain"))
    event_id = store(RiskEvent(account_id=account_id, outcome="risk"))
    result = asyncio.run(main.del_account(account_id))
    assert result["canceled_tasks"] == 10
    assert result["disabled_rules"] == 2
    assert result["disabled_monitors"] == 3
    with db.get_session() as session:
        assert session.get(DouyinAccount, account_id) is None
        for model, row_id in task_ids:
            row = session.get(model, row_id)
            assert row.status == "canceled" and row.scheduled_at is None
            assert row.next_allowed_at is None
        assert session.get(KeywordCollectionJob, job_id).cancel_requested
        for model, row_id in enabled_ids:
            assert not session.get(model, row_id).enabled
        assert session.get(PublishTask, history).status == "uncertain"
        assert session.get(RiskEvent, event_id) is not None
        assert session.get(AccountIdReservation, account_id) is not None
    main.engine.dm_automation.stop_account.assert_awaited_once_with(account_id)
    main.im_receiver.stop_account.assert_awaited_once_with(account_id)
    assert store(DouyinAccount(nickname="new")) > account_id


@pytest.mark.parametrize("model,status", [
    (PublishTask, "publishing"), (CommentTask, "doing"),
    (AccountActionTask, "doing"), (KeywordCollectionJob, "running"),
])
def test_inflight_account_deletion_keeps_everything(local_project, model, status):
    account_id = store(DouyinAccount())
    store(model(account_id=account_id, status=status))
    pending_id = store(PublishTask(account_id=account_id))
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.del_account(account_id))
    assert error.value.status_code == 409
    with db.get_session() as session:
        assert session.get(DouyinAccount, account_id)
        assert session.get(PublishTask, pending_id).status == "pending"


def test_shared_profile_is_not_removed(local_project):
    directory = local_project.root / "profiles" / "shared"
    directory.mkdir(parents=True)
    marker = directory / "keep.txt"
    marker.write_text("fixture", encoding="utf-8")
    old_id = store(DouyinAccount(profile_dir=str(directory)))
    store(DouyinAccount(profile_dir=str(directory)))
    result = asyncio.run(main.del_account(old_id))
    assert not result["profile_removed"]
    assert marker.exists()


def test_runtime_cleanup_failure_is_reported_without_undoing_delete(local_project):
    account_id = store(DouyinAccount())
    main.engine.dm_automation.stop_account.side_effect = RuntimeError("fixture")
    result = asyncio.run(main.del_account(account_id))
    assert result["ok"] and result["profile_cleanup_error"]
    with db.get_session() as session:
        assert session.get(DouyinAccount, account_id) is None


def test_waiting_operation_stops_after_account_disappears(local_project):
    account_id = store(DouyinAccount())
    engine = MonitorEngine(local_project.cfg, local_project.browser)

    async def scenario():
        lock = local_project.browser.lock_for(f"acc:{account_id}")
        await lock.acquire()

        async def operation():
            async with engine.operation_guard(account_id, OperationKind.READ_LIGHT):
                pytest.fail("deleted account must not reach the platform adapter")

        queued = asyncio.create_task(operation())
        await asyncio.sleep(0)
        with pytest.raises(HTTPException) as error:
            await main.del_account(account_id)
        assert error.value.status_code == 409
        with db.get_session() as session:
            session.delete(session.get(DouyinAccount, account_id))
            session.commit()
        lock.release()
        with pytest.raises(RuntimeError, match="账号已删除"):
            await queued

    asyncio.run(scenario())


def test_existing_database_reserves_orphan_ids_and_never_reuses(local_project):
    old_id = store(DouyinAccount(id=80))
    store(PublishTask(account_id=900, status="done"))
    with db._engine.begin() as connection:
        connection.execute(text("DELETE FROM douyinaccount WHERE id = :id"), {"id": old_id})
        connection.execute(text("DROP TABLE accountidreservation"))
    db._engine.dispose()
    db.init_db(str(local_project.path))
    new_id = store(DouyinAccount())
    assert new_id > 900
    asyncio.run(main.del_account(new_id))
    db._engine.dispose()
    db.init_db(str(local_project.path))
    assert store(DouyinAccount()) > new_id


@pytest.mark.parametrize("value,zone,expected", [
    (None, "Asia/Shanghai", None), ("", "Asia/Shanghai", None),
    ("2026-09-07T18:30", "Asia/Shanghai", datetime(2026, 9, 7, 10, 30)),
    ("2026-09-07T18:30+08:00", "America/New_York", datetime(2026, 9, 7, 10, 30)),
    ("2026-09-07T10:30:00Z", "Asia/Shanghai", datetime(2026, 9, 7, 10, 30)),
    ("2026-11-01T01:30-05:00", "America/New_York", datetime(2026, 11, 1, 6, 30)),
])
def test_schedule_normalizes_to_utc(value, zone, expected):
    assert parse_schedule(value, zone) == expected
    if expected:
        assert utc_iso(expected).endswith("Z")


@pytest.mark.parametrize("value,zone", [
    ("wrong", "Asia/Shanghai"), ("2026-09-07", "Asia/Shanghai"),
    ("2026-02-30T18:00", "Asia/Shanghai"),
    ("2026-03-08T02:30", "America/New_York"),
    ("2026-11-01T01:30", "America/New_York"),
    ("2026-09-07T18:00", "not/a-zone"),
])
def test_invalid_or_ambiguous_schedule_never_becomes_immediate(value, zone):
    with pytest.raises(ValueError):
        parse_schedule(value, zone)


def test_publish_validation_and_schedule_roundtrip(local_project):
    media = local_project.root / "fixture.jpg"
    media.write_bytes(b"fixture")
    account_id = store(DouyinAccount(platform="douyin", storage_state="{}"))
    body = main.PublishIn(account_id=account_id, media_paths=[str(media)], scheduled_at="invalid")
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.add_publish(body))
    assert error.value.status_code == 422
    with db.get_session() as session:
        assert not session.exec(select(PublishTask)).all()
    body.scheduled_at = "2026-09-07T18:30"
    result = asyncio.run(main.add_publish(body))
    assert result["scheduled_at"] == "2026-09-07T10:30:00Z"
    assert not result["schedule_needs_confirmation"]
    result = asyncio.run(main.update_publish(result["id"], main.PublishUpdate(scheduled_at=None)))
    assert result["scheduled_at"] is None


def test_legacy_schedule_migration_requires_confirmation(local_project):
    scheduled = datetime(2026, 9, 7, 18, 30)
    account_id = store(DouyinAccount(platform="douyin", storage_state="{}"))
    legacy_id = store(PublishTask(platform="douyin", account_id=account_id, scheduled_at=scheduled))
    deferred_id = store(PublishTask(scheduled_at=scheduled, next_allowed_at=scheduled, blocked_reason="cooldown"))
    with db._engine.begin() as connection:
        connection.execute(text("ALTER TABLE publishtask DROP COLUMN scheduled_at_is_utc"))
    db._engine.dispose()
    db.init_db(str(local_project.path))
    with db.get_session() as session:
        legacy, deferred = session.get(PublishTask, legacy_id), session.get(PublishTask, deferred_id)
        assert legacy.status == "draft" and legacy.scheduled_at == scheduled
        assert not legacy.scheduled_at_is_utc
        assert deferred.status == "pending" and deferred.scheduled_at_is_utc
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.update_publish(legacy_id, main.PublishUpdate(title="keep")))
    assert error.value.status_code == 422
    queue = asyncio.run(main.list_task_queue(platform="douyin"))
    assert queue["items"][0]["scheduled_at"] == "2026-09-07T18:30:00"
    result = asyncio.run(main.update_publish(legacy_id, main.PublishUpdate(scheduled_at="2026-09-07T18:30+08:00")))
    assert result["status"] == "pending" and result["scheduled_at"] == "2026-09-07T10:30:00Z"
    db._engine.dispose()
    db.init_db(str(local_project.path))
    with db.get_session() as session:
        assert session.get(PublishTask, legacy_id).scheduled_at_is_utc


def test_interrupted_schedule_migration_stays_conservative(local_project):
    task_id = store(PublishTask(scheduled_at=datetime(2026, 9, 7, 18, 30)))
    with db._engine.begin() as connection:
        connection.execute(text("ALTER TABLE publishtask DROP COLUMN scheduled_at_is_utc"))
    # Simulate a restart between the schema change and the backfill.
    db._auto_migrate(db._engine)
    db._engine.dispose()
    db.init_db(str(local_project.path))
    with db.get_session() as session:
        row = session.get(PublishTask, task_id)
        assert not row.scheduled_at_is_utc and row.status == "draft"


def test_relay_validates_before_creation_and_persists_schedule_atomically(local_project, monkeypatch):
    account_id = store(DouyinAccount(platform="douyin", storage_state="{}"))
    content_id = store(ContentRecord(target_id=0, aweme_id="fixture", download_status="done"))
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    monkeypatch.setattr(main, "engine", engine)
    files = [str(local_project.root / "fixture.jpg")]
    monkeypatch.setattr(engine, "_content_files", lambda row: files)
    body = main.RepostIn(account_id=account_id, scheduled_at="invalid")
    with pytest.raises(HTTPException) as error:
        asyncio.run(main._repost_content(content_id, body, "douyin"))
    assert error.value.status_code == 422
    with db.get_session() as session:
        assert not session.exec(select(PublishTask)).all()
    body.scheduled_at = "2026-09-07T18:30+08:00"
    result = asyncio.run(main._repost_content(content_id, body, "douyin"))
    with db.get_session() as session:
        row = session.get(PublishTask, result["task_id"])
        assert row.scheduled_at == datetime(2026, 9, 7, 10, 30)
        assert row.scheduled_at_is_utc and json.loads(row.media_json) == files


def test_pending_legacy_schedules_are_held_by_scheduler(local_project, monkeypatch):
    task_id = store(PublishTask(scheduled_at=datetime(2020, 1, 1), scheduled_at_is_utc=False))
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    publish = AsyncMock()
    monkeypatch.setattr(engine, "publish_task", publish)
    asyncio.run(engine._process_publish())
    publish.assert_not_awaited()
    with db.get_session() as session:
        assert session.get(PublishTask, task_id).status == "draft"


@pytest.mark.parametrize("status", ["draft", "canceled", "uncertain", "done", "publishing"])
def test_publish_nonrunnable_states_do_not_reach_browser(local_project, status):
    task_id = store(PublishTask(status=status))
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    result = asyncio.run(engine._publish_task_locked(task_id))
    assert not result["ok"] and status in result["error"]
    with db.get_session() as session:
        assert session.get(PublishTask, task_id).status == status


def test_unsafe_delete_and_comment_review_shortcut_are_rejected(local_project):
    task_id = store(CommentTask(status="uncertain"))
    pending_id = store(CommentTask())
    for operation in (main.run_comment_task_now(task_id), main.del_comment_task(task_id),
                      main.batch_del_comment_tasks(main.IdsIn2(ids=[pending_id, task_id]))):
        with pytest.raises(HTTPException):
            asyncio.run(operation)
    with db.get_session() as session:
        assert session.get(CommentTask, pending_id) is not None
        assert session.get(CommentTask, task_id).status == "uncertain"
    for status in ("draft", "canceled"):
        with pytest.raises(HTTPException):
            asyncio.run(main.run_comment_task_now(store(CommentTask(status=status))))
    for status in ("publishing", "uncertain"):
        with pytest.raises(HTTPException) as error:
            asyncio.run(main.del_publish(store(PublishTask(status=status))))
        assert error.value.status_code == 409


@pytest.mark.parametrize("queue,model", [("publishes", PublishTask), ("comments", CommentTask), ("actions", AccountActionTask)])
@pytest.mark.parametrize("outcome", ["done", "canceled"])
def test_human_resolution_is_audited_and_never_retries(local_project, queue, model, outcome):
    account_id = store(DouyinAccount())
    task_id = store(model(account_id=account_id, status="uncertain", error="interrupted"))
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 8000)})
    body = main.TaskResolutionIn(outcome=outcome, note="平台核对完成")
    assert asyncio.run(main.resolve_task_result(queue, task_id, body, request))["status"] == outcome
    with db.get_session() as session:
        row = session.get(model, task_id)
        assert row.status == outcome and row.scheduled_at is None
        assert (row.done_at is not None) == (outcome == "done")
        audit = session.exec(select(RiskAdminAudit)).one()
        assert json.loads(audit.detail)["previous_error"] == "interrupted"
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.resolve_task_result(queue, task_id, body, request))
    assert error.value.status_code == 409


def test_resolution_http_contract_and_admin_gate(local_project, monkeypatch):
    task_id = store(PublishTask(status="uncertain"))
    monkeypatch.setenv("CREATORHUB_ADMIN_TOKEN", "fixture-token")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1") as client:
            path = f"/api/task-queue/publishes/{task_id}/resolve"
            data = {"outcome": "done", "note": "Checked the platform result"}
            response = await client.post(path, json=data)
            assert response.status_code == 403
            headers = {"X-CreatorHub-Admin-Token": "fixture-token", "X-CreatorHub-Actor": "fixture-user"}
            response = await client.post(path, json={**data, "outcome": "pending"}, headers=headers)
            assert response.status_code == 422
            response = await client.post(path, json={"outcome": "done"}, headers=headers)
            assert response.status_code == 422
            with db.get_session() as session:
                assert session.get(PublishTask, task_id).status == "uncertain"
                assert not session.exec(select(RiskAdminAudit)).all()
            response = await client.post(path, json=data, headers=headers)
            assert response.status_code == 200
            with db.get_session() as session:
                assert session.exec(select(RiskAdminAudit)).one().actor == "fixture-user"

    asyncio.run(scenario())


def test_overview_uses_all_rows_and_platform_isolation(local_project):
    with db.get_session() as session:
        session.add(DouyinAccount(platform="douyin"))
        session.add(MonitorTarget(platform="douyin", enabled=True))
        session.add(MonitorTarget(platform="douyin", enabled=False))
        for i in range(125):
            session.add(ContentRecord(platform="douyin", target_id=0, aweme_id=str(i), download_status="done"))
            session.add(CommentRecord(platform="douyin", aweme_id="work", comment_id=str(i)))
        session.add(ContentRecord(platform="douyin", target_id=0, aweme_id="pending"))
        session.add(ContentRecord(platform="xhs", target_id=0, aweme_id="other", download_status="done"))
        session.commit()
    result = asyncio.run(main.overview_summary("douyin"))
    assert result == {"platform": "douyin", "accounts": 1, "monitors": 1, "downloaded": 125, "comments": 125}


def test_engine_stop_waits_for_loop_cleanup(local_project):
    engine = MonitorEngine(local_project.cfg, local_project.browser)

    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()

        async def loop():
            try:
                started.set()
                await asyncio.Future()
            finally:
                await asyncio.sleep(0.01)
                stopped.set()

        engine._loop = loop
        engine.start()
        await started.wait()
        await engine.stop()
        assert stopped.is_set() and engine._task is None

    asyncio.run(scenario())


@pytest.mark.parametrize("model", [PublishTask, CommentTask, AccountActionTask])
def test_risk_defer_and_wakeup_never_overwrite_user_appointment(local_project, model):
    account_id = store(DouyinAccount(status="active"))
    now = datetime.utcnow()
    original = now + timedelta(days=1)
    row = model(account_id=account_id, scheduled_at=original)
    MonitorEngine._defer_row(row, "network hold", now + timedelta(minutes=5))
    assert row.scheduled_at == original
    MonitorEngine._defer_row(row, "shorter hold", now + timedelta(minutes=1))
    assert row.next_allowed_at == now + timedelta(minutes=5)
    row.scheduled_at = original + timedelta(days=1)  # user edits a blocked task
    row_id = store(row)
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    assert engine._wake_deferred_tasks(account_id) == 1
    with db.get_session() as session:
        saved = session.get(model, row_id)
        assert saved.scheduled_at == original + timedelta(days=1)
        assert saved.next_allowed_at is None and not saved.blocked_reason
        assert not engine._task_due(saved, now)


@pytest.mark.parametrize("scheduled_delta,policy_delta,expected", [
    (None, None, True), (-1, -1, True), (0, 0, True),
    (60, None, False), (None, 60, False), (-1, 60, False), (60, -1, False),
])
def test_task_due_requires_both_schedule_and_policy_deadlines(scheduled_delta, policy_delta, expected):
    now = datetime.utcnow()
    when = lambda delta: None if delta is None else now + timedelta(seconds=delta)
    row = PublishTask(scheduled_at=when(scheduled_delta), next_allowed_at=when(policy_delta))
    assert MonitorEngine._task_due(row, now) is expected


@pytest.mark.parametrize("model,method", [
    (PublishTask, "publish_task"), (CommentTask, "execute_comment_task"),
    (AccountActionTask, "execute_action_task"),
])
def test_manual_run_does_not_override_policy_deferral(local_project, monkeypatch, model, method):
    account_id = store(DouyinAccount(status="active"))
    deadline = datetime.utcnow() + timedelta(minutes=5)
    task_id = store(model(account_id=account_id, next_allowed_at=deadline, blocked_reason="network hold"))
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    adapter_gate = AsyncMock(side_effect=AssertionError("deferred task reached the adapter"))
    monkeypatch.setattr(engine, "_native_write_environment_error", adapter_gate)
    result = asyncio.run(getattr(engine, method)(task_id))
    assert result["deferred"] and result["next_allowed_at"] == utc_iso(deadline)
    adapter_gate.assert_not_called()
    with db.get_session() as session:
        assert session.get(model, task_id).status == "pending"


@pytest.mark.parametrize("model,process,execute", [
    (PublishTask, "_process_publish", "publish_task"),
    (CommentTask, "_process_comment_tasks", "execute_comment_task"),
    (AccountActionTask, "_process_action_tasks", "execute_action_task"),
])
def test_schedulers_skip_tasks_with_future_policy_deadlines(local_project, monkeypatch, model, process, execute):
    account_id = store(DouyinAccount(status="active"))
    store(model(account_id=account_id, next_allowed_at=datetime.utcnow() + timedelta(minutes=5)))
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    run = AsyncMock()
    monkeypatch.setattr(engine, execute, run)
    asyncio.run(getattr(engine, process)())
    run.assert_not_awaited()


@pytest.mark.parametrize("invalid", ["empty", "missing", "platform", "account", "status"])
def test_batch_approval_rejects_changed_scope_without_partial_approval(local_project, invalid):
    first = store(CommentTask(platform="douyin", account_id=1, status="draft"))
    second = store(CommentTask(platform="xhs" if invalid == "platform" else "douyin",
                               account_id=2 if invalid == "account" else 1,
                               status="pending" if invalid == "status" else "draft"))
    ids = [] if invalid == "empty" else [first, 999999 if invalid == "missing" else second]
    body = main.CommentBatchApproveIn(ids=ids, platform="douyin", account_id=1)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.batch_approve_comment_tasks(body))
    assert error.value.status_code == (422 if invalid == "empty" else 409)
    with db.get_session() as session:
        assert session.get(CommentTask, first).status == "draft"
        assert session.get(CommentTask, second).status == ("pending" if invalid == "status" else "draft")


def test_batch_approval_requires_platform_over_http_and_deduplicates(local_project):
    first = store(CommentTask(platform="douyin", status="draft"))
    second = store(CommentTask(platform="xhs", status="draft"))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1") as client:
            path = "/api/comment-tasks/batch-approve"
            for body in ({"ids": [first]}, {"ids": [], "platform": "douyin"},
                         {"ids": [-1], "platform": "douyin"}, {"ids": [first], "platform": "invalid"},
                         {"ids": [first] * 501, "platform": "douyin"}):
                response = await client.post(path, json=body)
                assert response.status_code == 422
            response = await client.post(path, json={"ids": [first, first], "platform": "douyin"})
            assert response.status_code == 200 and response.json()["approved"] == 1

    asyncio.run(scenario())
    with db.get_session() as session:
        assert session.get(CommentTask, first).status == "pending"
        assert session.get(CommentTask, second).status == "draft"
