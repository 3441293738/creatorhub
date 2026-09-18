"""Second-level monitor intervals, isolated API requests and scheduler clocks."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from sqlmodel import select

import app.db as db
import app.main as main
import app.engine.monitor as monitor
from app.engine.cadence import row_deadline
from app.engine.monitor import MonitorEngine
from app.models import CommentWatch, DanmakuWatch, DouyinAccount, MonitorTarget
from test_project_optimizations import local_project, store


@pytest.fixture(params=[("monitors", MonitorTarget), ("comment-watches", CommentWatch),
                        ("danmaku-watches", DanmakuWatch)])
def interval_api(local_project, monkeypatch, request):
    endpoint, model = request.param
    account = store(DouyinAccount(status="active"))
    monkeypatch.setattr(main, "resolve_sec_uid", AsyncMock(return_value="TARGET"))
    monkeypatch.setattr(main, "resolve_aweme_id", AsyncMock(return_value="TARGET"))
    body = ({"url_or_secuid": "TARGET"} if model is MonitorTarget else
            {"url_or_id": "TARGET", "kind": "user"})
    return SimpleNamespace(path="/api/" + endpoint, model=model,
                           body={**body, "account_id": account})


def request(method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://127.0.0.1") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


@pytest.mark.parametrize("seconds", [1, 5, 10, 15, 30, 59, 90, 300, 86400])
def test_create_and_edit_keep_exact_seconds(interval_api, seconds):
    f = interval_api
    response = request("POST", f.path, json={**f.body, "interval_seconds": seconds})
    assert response.status_code == 200, response.text
    row_id = response.json()["id"]
    assert response.json()["interval_seconds"] == seconds
    edited = 7 if seconds != 7 else 19
    response = request("PUT", f"{f.path}/{row_id}", json={"interval_seconds": edited})
    assert response.status_code == 200 and response.json()["interval_seconds"] == edited
    with db.get_session() as session:
        assert session.get(f.model, row_id).interval_seconds == edited


@pytest.mark.parametrize("seconds", [-1, 86401])
def test_invalid_ranges_do_not_create_or_change_a_monitor(interval_api, seconds):
    f = interval_api
    response = request("POST", f.path, json={**f.body, "interval_seconds": seconds})
    assert response.status_code == 400
    row_id = store(f.model(interval_seconds=300))
    response = request("PUT", f"{f.path}/{row_id}", json={"interval_seconds": seconds})
    assert response.status_code == 400
    with db.get_session() as session:
        rows = session.exec(select(f.model)).all()
        assert len(rows) == 1 and rows[0].interval_seconds == 300


def test_zero_only_means_global_for_danmaku(interval_api):
    f = interval_api
    response = request("POST", f.path, json={**f.body, "interval_seconds": 0})
    if f.model is DanmakuWatch:
        assert response.status_code == 200 and response.json()["uses_global_interval"]
        assert response.json()["effective_interval_seconds"] == main.cfg.engine.scan_interval_seconds
    else:
        assert response.status_code == 400


@pytest.mark.parametrize("seconds", [1, 5, 30, 75, 300])
def test_global_seconds_persist_without_changing_existing_monitor_intervals(local_project, seconds):
    from app.engine_settings import load_persisted_engine_settings
    from app.config import Config
    target = store(MonitorTarget(interval_seconds=600))
    response = request("PUT", "/api/settings/engine", json={"scan_interval_seconds": seconds})
    assert response.status_code == 200
    restarted = Config()
    assert load_persisted_engine_settings(restarted)
    assert restarted.engine.scan_interval_seconds == seconds
    with db.get_session() as session:
        assert session.get(MonitorTarget, target).interval_seconds == 600


@pytest.mark.parametrize("model,kind", [(MonitorTarget, "monitor"),
    (CommentWatch, "comment_watch"), (DanmakuWatch, "danmaku")])
def test_second_deadline_keeps_jitter_and_never_runs_early(local_project, model, kind):
    anchor = datetime(2026, 9, 18)
    row = model(id=1, interval_seconds=5, last_scan_at=anchor, created_at=anchor)
    deadline = row_deadline(row, local_project.cfg, kind=kind)
    assert anchor + timedelta(seconds=5) <= deadline <= anchor + timedelta(seconds=5.75)
    assert row_deadline(row, local_project.cfg, kind=kind) == deadline


@pytest.mark.parametrize("model", [MonitorTarget, CommentWatch, DanmakuWatch])
@pytest.mark.parametrize("seconds", [1, 5, 59, 77, 90, 86399])
def test_only_enabled_second_precision_monitors_use_fast_polling(local_project, model, seconds):
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    row_id = store(model(interval_seconds=300))
    assert engine._monitor_poll_seconds() == 15
    with db.get_session() as session:
        row = session.get(model, row_id)
        row.interval_seconds = seconds
        session.add(row); session.commit()
    assert engine._monitor_poll_seconds() == 1
    with db.get_session() as session:
        row = session.get(model, row_id)
        row.enabled = False
        session.add(row); session.commit()
    assert engine._monitor_poll_seconds() == 15


@pytest.mark.parametrize("seconds", [5, 77, 90])
def test_danmaku_polling_uses_global_seconds_only_when_inherited(local_project, seconds):
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    local_project.cfg.engine.scan_interval_seconds = seconds
    assert engine._monitor_poll_seconds() == 15
    row_id = store(DanmakuWatch(interval_seconds=0))
    assert engine._monitor_poll_seconds() == 1
    with db.get_session() as session:
        row = session.get(DanmakuWatch, row_id)
        row.interval_seconds = 300
        session.add(row); session.commit()
    assert engine._monitor_poll_seconds() == 15


def test_fast_polling_does_not_speed_up_maintenance_or_write_queues(local_project, monkeypatch):
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    engine._running = True
    clock = [0.0]
    monkeypatch.setattr(monitor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(engine, "_monitor_poll_seconds", lambda: 1)
    engine._prune_risk_events_if_due = Mock()
    for name in ("_collect_idle_browser_sessions", "_scan_once", "_scan_comment_watches",
                 "_scan_danmaku_watches", "_retry_failed", "_process_risk_recovery",
                 "_check_accounts", "_check_work_health", "_process_xhs_dm_automation",
                 "_process_publish", "_process_comment_rules", "_process_comment_tasks",
                 "_process_action_tasks", "_process_collection_jobs"):
        monkeypatch.setattr(engine, name, AsyncMock())
    async def sleep(delay):
        assert delay == 1
        clock[0] += delay
        if clock[0] >= 17:
            engine._running = False
    monkeypatch.setattr(monitor.asyncio, "sleep", sleep)
    asyncio.run(engine._loop())
    for name in ("_scan_once", "_scan_comment_watches", "_scan_danmaku_watches"):
        assert getattr(engine, name).await_count == 17
    for name in ("_retry_failed", "_process_risk_recovery", "_check_accounts", "_process_publish",
                 "_process_comment_rules", "_process_comment_tasks", "_process_action_tasks"):
        assert getattr(engine, name).await_count == 2
