"""Offline regressions for automatic timing and background risk boundaries."""
import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from sqlmodel import select

import app.db as db
import app.main as main
import app.engine.monitor as monitor
from app.config import Config, load_config
from app.engine.cadence import periodic_deadline, row_deadline
from app.engine.monitor import MonitorEngine
from app.models import (AccountRiskState, CommentRule, CommentTask, CommentWatch,
                        DanmakuWatch, DouyinAccount, KeywordCollectionJob, MonitorTarget)
from app.risk import OperationKind
from app.risk_admin import (RiskSettingsError, apply_risk_settings, export_risk_settings,
                            load_persisted_risk_settings, save_risk_settings)
from app.scheduling import utc_iso
from test_project_optimizations import local_project, store


def test_periodic_jitter_is_stable_positive_and_distinct_per_task():
    anchor = datetime(2026, 9, 7, 2)
    deadlines = []
    for index in range(80):
        args = {"key": f"monitor:{index}", "jitter": 0.15}
        deadline = periodic_deadline(anchor, 300, **args)
        assert anchor + timedelta(seconds=300) <= deadline <= anchor + timedelta(seconds=345)
        assert all(periodic_deadline(anchor, 300, **args) == deadline for _ in range(10))
        deadlines.append(deadline)
    assert len(set(deadlines)) == 80
    assert periodic_deadline(anchor, 300, key="fixture", jitter=0) == anchor + timedelta(seconds=300)


@pytest.mark.parametrize("jitter", [-1, 4, float("nan"), float("inf")])
def test_invalid_legacy_jitter_never_shortens_or_creates_unbounded_wait(jitter):
    anchor = datetime(2026, 9, 7, 2)
    deadline = periodic_deadline(anchor, 300, key="fixture", jitter=jitter)
    assert anchor + timedelta(seconds=300) <= deadline <= anchor + timedelta(seconds=600)


@pytest.mark.parametrize("model,kind,processor,runner,serializer", [
    (MonitorTarget, "monitor", "_scan_once", "scan_target", "_target_dict"),
    (CommentWatch, "comment_watch", "_scan_comment_watches", "scan_comment_watch", "_watch_dict"),
    (DanmakuWatch, "danmaku", "_scan_danmaku_watches", "scan_danmaku_watch", "_danmaku_watch_dict"),
    (CommentRule, "comment_rule", "_process_comment_rules", "run_comment_rule", "_rule_dict"),
])
def test_first_run_deadline_matches_ui_and_survives_engine_restart(
        local_project, monkeypatch, model, kind, processor, runner, serializer):
    cfg = local_project.cfg
    cfg.engine.initial_scan_spread_seconds = 60
    anchor = datetime(2026, 9, 7, 2)
    row_id = store(model(account_id=store(DouyinAccount()), created_at=anchor, enabled=True))
    with db.get_session() as session:
        row = session.get(model, row_id)
        deadline = row_deadline(row, cfg, kind=kind)
    assert anchor < deadline <= anchor + timedelta(seconds=60)
    assert getattr(main, serializer)(row)["next_auto_run_at"] == utc_iso(deadline)

    class Clock(datetime):
        value = anchor

        @classmethod
        def utcnow(cls):
            return cls.value

    monkeypatch.setattr(monitor, "datetime", Clock)
    for _ in range(2):
        engine = MonitorEngine(cfg, local_project.browser)
        run = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(engine, runner, run)
        Clock.value = deadline - timedelta(microseconds=1)
        asyncio.run(getattr(engine, processor)())
        run.assert_not_awaited()
        Clock.value = deadline
        asyncio.run(getattr(engine, processor)())
        run.assert_awaited_once_with(row_id)
    row.enabled = False
    assert row_deadline(row, cfg, kind=kind) is None


def test_periodic_checks_and_keepalive_do_not_re_roll_or_advance(local_project, monkeypatch):
    cfg = local_project.cfg
    cfg.engine.scan_jitter = 0.4
    engine = MonitorEngine(cfg, local_project.browser)
    last = datetime.utcnow() - timedelta(seconds=299)
    monkeypatch.setattr(monitor.random, "uniform", Mock(side_effect=AssertionError("re-rolled timer")))
    for _ in range(10):
        assert not engine._due(last, 300, key="fixture")
        assert not engine._keepalive_due(datetime.utcnow() - timedelta(hours=5.9), account_id=1)
    old = MonitorTarget(id=1, created_at=datetime.utcnow() - timedelta(days=1))
    assert engine._periodic_due(old, "monitor")  # overdue tasks are not postponed on every restart


def test_manual_scan_does_not_wait_for_new_monitor_spread(local_project, monkeypatch):
    account_id = store(DouyinAccount(status="active"))
    row_id = store(MonitorTarget(account_id=account_id))
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    scan = AsyncMock(return_value={"ok": True, "new": 0})
    monkeypatch.setattr(engine, "_scan_target_locked", scan)
    assert asyncio.run(engine.scan_target(row_id))["ok"]
    scan.assert_awaited_once_with(row_id)


def test_generated_comment_gaps_are_positive_and_approval_preserves_them(local_project, monkeypatch):
    cfg = local_project.cfg
    cfg.engine.quiet_hours_enabled = False
    cfg.engine.comment_jitter = 0.4
    account_id = store(DouyinAccount(platform="douyin", status="active", storage_state="{}"))
    rule_id = store(CommentRule(platform="douyin", account_id=account_id,
                               require_review=True, min_gap_seconds=1800,
                               max_per_run=4, daily_cap=10, templates='["thanks"]'))
    engine = MonitorEngine(cfg, local_project.browser)
    candidates = [{"aweme_id": f"work-{i}", "target_comment_id": f"comment-{i}",
                   "ctx": {}, "source_text": "hello"} for i in range(4)]
    monkeypatch.setattr(local_project.browser, "identity_for", lambda account: None, raising=False)
    monkeypatch.setattr(engine, "_discover_targets", AsyncMock(return_value=(candidates, "")))
    monkeypatch.setattr(monitor.random, "uniform", lambda low, high: low)
    monkeypatch.setattr(monitor.random, "randint", lambda low, high: high)
    before = datetime.utcnow()
    result = asyncio.run(engine.run_comment_rule(rule_id))
    assert result["created"] == 4
    with db.get_session() as session:
        rows = session.exec(select(CommentTask).where(CommentTask.rule_id == rule_id).order_by(CommentTask.id)).all()
        times = {row.id: row.scheduled_at for row in rows}
        assert all(row.status == "draft" for row in rows)
    sequence = [before, *times.values()]
    assert all((b - a).total_seconds() >= 1800 for a, b in zip(sequence, sequence[1:]))
    asyncio.run(main.batch_approve_comment_tasks(main.CommentBatchApproveIn(
        ids=list(times), platform="douyin", account_id=account_id)))
    with db.get_session() as session:
        for row_id, when in times.items():
            assert session.get(CommentTask, row_id).scheduled_at == when
            assert session.get(CommentTask, row_id).status == "pending"


@pytest.mark.parametrize("trigger", ["push", "reconnect", "scheduled"])
@pytest.mark.parametrize("hold", ["manual_review_required", "cooldown_until", "retry_not_before", "session_rest_until"])
def test_dm_automatic_wakes_obey_hard_risk_holds(local_project, monkeypatch, trigger, hold):
    account_id = store(DouyinAccount(platform="xhs", status="active", storage_state="{}"))
    with db.get_session() as session:
        state = AccountRiskState(account_id=account_id)
        setattr(state, hold, True if hold == "manual_review_required" else datetime.utcnow() + timedelta(hours=1))
        session.add(state); session.commit()
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    poll = AsyncMock(side_effect=AssertionError("held background wake opened a browser"))
    monkeypatch.setattr(engine.dm_automation, "poll", poll)
    result = asyncio.run(engine.poll_xhs_dm_now(account_id, trigger=trigger))
    assert result["skipped"]
    poll.assert_not_awaited()


@pytest.mark.parametrize("due", [False, True])
def test_dm_scheduler_does_not_bootstrap_outside_guard(local_project, monkeypatch, due):
    cfg = local_project.cfg
    cfg.engine.xhs_dm_monitor_enabled = True
    account_id = store(DouyinAccount(platform="xhs", status="active", storage_state="{}"))
    with db.get_session() as session:
        session.add(AccountRiskState(account_id=account_id, manual_review_required=True)); session.commit()
    engine = MonitorEngine(cfg, local_project.browser)
    bootstrap = AsyncMock(side_effect=AssertionError("unguarded bootstrap"))
    monkeypatch.setattr(engine.dm_automation, "ensure_realtime", bootstrap)
    monkeypatch.setattr(engine.dm_automation, "due", lambda aid: due)
    asyncio.run(engine._process_xhs_dm_automation())
    bootstrap.assert_not_awaited()


def test_collection_scheduler_skips_deferred_and_manually_held_accounts(local_project, monkeypatch):
    held = store(DouyinAccount(status="active", platform="xhs", storage_state="{}"))
    ready = store(DouyinAccount(status="active", platform="xhs", storage_state="{}"))
    store(KeywordCollectionJob(account_id=ready, next_allowed_at=datetime.utcnow() + timedelta(hours=1)))
    store(KeywordCollectionJob(account_id=held))
    expected = store(KeywordCollectionJob(account_id=ready))
    with db.get_session() as session:
        session.add(AccountRiskState(account_id=held, manual_review_required=True)); session.commit()
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    enqueue = Mock(return_value=True)
    monkeypatch.setattr(engine, "enqueue_collection_job", enqueue)
    asyncio.run(engine._process_collection_jobs())
    enqueue.assert_called_once_with(expected)


@pytest.mark.parametrize("status,future", [("pending", True), ("running", False), ("done", False)])
def test_direct_collection_execution_respects_deadline_and_state(local_project, monkeypatch, status, future):
    account_id = store(DouyinAccount(status="active", platform="xhs", storage_state="{}"))
    job_id = store(KeywordCollectionJob(account_id=account_id, platform="xhs", status=status,
                 next_allowed_at=datetime.utcnow() + timedelta(minutes=5) if future else None))
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    run = AsyncMock(side_effect=AssertionError("collection unexpectedly executed"))
    monkeypatch.setattr(engine.keyword_collector, "run", run)
    result = asyncio.run(engine.run_collection_job(job_id))
    assert result.get("deferred") if future else not result["ok"]
    run.assert_not_awaited()
    with db.get_session() as session:
        assert session.get(KeywordCollectionJob, job_id).status == status


def test_network_jitter_only_extends_persisted_deadline(local_project):
    account_id = store(DouyinAccount(status="active"))
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    engine.risk._rng = Mock(uniform=lambda low, high: high)
    now = datetime(2026, 9, 7, 2)
    first = engine.risk.record_failure(account_id, OperationKind.READ_LIGHT, TimeoutError(), now=now)
    assert first.next_allowed_at == now + timedelta(seconds=330)
    engine.risk._rng = Mock(uniform=lambda low, high: low)
    second = engine.risk.record_failure(account_id, OperationKind.READ_LIGHT, TimeoutError(), now=now + timedelta(seconds=1))
    assert second.next_allowed_at == first.next_allowed_at
    with db.get_session() as session:
        assert session.get(AccountRiskState, account_id).retry_not_before == first.next_allowed_at


def test_cadence_settings_round_trip_and_invalid_updates_are_atomic(local_project):
    cfg = local_project.cfg
    patch = {"scan_jitter": 0.25, "comment_jitter": 0.5, "xhs_dm_poll_jitter": 0.2,
             "initial_scan_spread_seconds": 90}
    apply_risk_settings(cfg, {"schedule": patch, "risk_control": {"network_retry_jitter_seconds": 45}})
    save_risk_settings(cfg)
    restored = Config()
    assert load_persisted_risk_settings(restored)
    assert all(getattr(restored.engine, key) == value for key, value in patch.items())
    assert restored.risk_control.network_retry_jitter_seconds == 45
    before = export_risk_settings(cfg)
    for key in ("scan_jitter", "comment_jitter", "xhs_dm_poll_jitter"):
        for value in (-1, 1.01, True, "nan", float("inf")):
            with pytest.raises(RiskSettingsError):
                apply_risk_settings(cfg, {"schedule": {key: value}})
            assert export_risk_settings(cfg) == before
    for value in (1.5, float("inf"), -1, 601):
        with pytest.raises(RiskSettingsError):
            apply_risk_settings(cfg, {"schedule": {"initial_scan_spread_seconds": value}})
        assert export_risk_settings(cfg) == before


def test_yaml_clamps_legacy_jitter_to_positive_bounded_parameters(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("engine:\n  scan_jitter: -1\n  comment_jitter: 5\n  initial_scan_spread_seconds: 9999\n"
                    + f"  media_dir: {json.dumps(str(tmp_path / 'media'))}\n"
                    + f"  profiles_dir: {json.dumps(str(tmp_path / 'profiles'))}\n"
                    + f"storage:\n  db_path: {json.dumps(str(tmp_path / 'local.db'))}\n", encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.engine.scan_jitter == 0 and cfg.engine.comment_jitter == 1
    assert cfg.engine.initial_scan_spread_seconds == 600


def test_dm_rule_crud_uses_its_own_serializer(local_project):
    account_id = store(DouyinAccount(platform="xhs"))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1") as client:
            body = {"account_id": account_id, "name": "fixture", "match_mode": "keywords",
                    "keywords": ["hello"], "reply_templates": ["thanks"], "review_before_send": True,
                    "min_delay_seconds": 90, "max_delay_seconds": 240}
            response = await client.post("/api/dm/auto-reply-rules", json=body)
            assert response.status_code == 200
            saved = response.json()
            assert saved["min_delay_seconds"] == 90 and saved["max_delay_seconds"] == 240
            response = await client.get("/api/dm/auto-reply-rules", params={"account_id": account_id})
            assert response.status_code == 200 and response.json()[0]["keywords"] == ["hello"]
            response = await client.put(f"/api/dm/auto-reply-rules/{saved['id']}", json={**body, "max_delay_seconds": 300})
            assert response.status_code == 200 and response.json()["max_delay_seconds"] == 300
            # The similarly named comment serializer must still work too.
            assert main._rule_dict(CommentRule())["mode"] == "auto_reply"

    asyncio.run(scenario())
