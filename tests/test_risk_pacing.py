"""Offline account pacing, persistent holds and browser-only health checks."""
import asyncio
import json
import random
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import select

import app.db as db
import app.main as main
import app.engine.monitor as monitor
from app.engine.monitor import MonitorEngine
from app.models import AccountRiskState, DouyinAccount, RiskEvent
from app.risk import OperationKind, RiskController
from app.risk_admin import (RiskSettingsError, apply_risk_settings,
                            export_risk_settings, load_persisted_risk_settings,
                            save_risk_settings)
from test_project_optimizations import local_project, store


@pytest.fixture
def paced(local_project, monkeypatch):
    cfg = local_project.cfg
    cfg.engine.quiet_hours_enabled = False
    cfg.engine.verify_proxy_region = False
    cfg.engine.work_health_stat_snapshots = False
    cfg.risk_control.read_light_gap_seconds = 0
    cfg.risk_control.read_heavy_gap_seconds = 0
    account_id = store(DouyinAccount(platform="xhs", status="active", storage_state=json.dumps({
        "cookies": [{"name": "web_session", "value": "fixture"}]})))
    engine = MonitorEngine(cfg, local_project.browser)
    engine.risk._rng = random.Random(7)
    monkeypatch.setattr(main, "engine", engine)
    return SimpleNamespace(project=local_project, cfg=cfg, engine=engine,
                           risk=engine.risk, account_id=account_id)


def risk_state(account_id):
    with db.get_session() as session:
        return session.get(AccountRiskState, account_id)


def test_deadline_sampled_once_and_retained_across_restart(paced):
    now = datetime(2026, 9, 7, 2)
    paced.risk.record_success(paced.account_id, OperationKind.READ_HEAVY, now=now)
    deadline = risk_state(paced.account_id).operation_not_before
    assert now + timedelta(seconds=8) <= deadline <= now + timedelta(seconds=25)
    paced.risk._rng = Mock(uniform=Mock(side_effect=AssertionError("poll must not resample")))
    for seconds in (1, 2, 3):
        decision = paced.risk.preflight(paced.account_id, OperationKind.READ_LIGHT,
                                       now=now + timedelta(seconds=seconds))
        assert decision.signal == "operation_pacing" and decision.next_allowed_at == deadline
    db._engine.dispose()
    db.init_db(str(paced.project.path))
    restarted = RiskController(paced.cfg)
    assert restarted.preflight(paced.account_id, OperationKind.READ_LIGHT, now=now).next_allowed_at == deadline
    assert restarted.preflight(paced.account_id, OperationKind.READ_LIGHT, now=deadline).allowed
    other_id = store(DouyinAccount(status="active"))
    assert restarted.preflight(other_id, OperationKind.READ_LIGHT, now=now).allowed


def test_failures_count_toward_rest_and_clear_retains_rest(paced):
    policy = paced.cfg.risk_control
    policy.operation_gap_min_seconds = policy.operation_gap_max_seconds = 2
    policy.session_operation_limit = 3
    policy.session_rest_min_seconds = policy.session_rest_max_seconds = 30
    now = datetime(2026, 9, 7, 2)
    paced.risk.record_success(paced.account_id, OperationKind.READ_LIGHT, now=now)
    paced.risk.record_success(paced.account_id, OperationKind.READ_HEAVY, now=now + timedelta(seconds=2))
    paced.risk.record_failure(paced.account_id, OperationKind.READ_LIGHT, "content rejected",
                             now=now + timedelta(seconds=4))
    state = risk_state(paced.account_id)
    assert state.session_operation_count == 3
    assert state.session_rest_until == now + timedelta(seconds=34)
    decision = paced.risk.preflight(paced.account_id, OperationKind.READ_HEAVY, now=now + timedelta(seconds=6))
    assert decision.signal == "session_rest"
    paced.risk.clear_account(paced.account_id)
    assert paced.risk.next_write_at(paced.account_id) == state.session_rest_until
    assert not paced.risk.preflight(paced.account_id, OperationKind.READ_LIGHT, now=now + timedelta(seconds=33)).allowed
    assert paced.risk.preflight(paced.account_id, OperationKind.READ_LIGHT, now=state.session_rest_until).allowed
    paced.risk.record_success(paced.account_id, OperationKind.READ_LIGHT, now=state.session_rest_until)
    assert risk_state(paced.account_id).session_operation_count == 1
    assert risk_state(paced.account_id).session_rest_until is None


def test_natural_idle_resets_session_without_restarting_service(paced):
    now = datetime(2026, 9, 7, 2)
    paced.cfg.risk_control.session_operation_limit = 2
    paced.risk.record_success(paced.account_id, OperationKind.READ_LIGHT, now=now)
    paced.risk.record_success(paced.account_id, OperationKind.READ_LIGHT, now=now + timedelta(minutes=4))
    assert risk_state(paced.account_id).session_operation_count == 1
    assert risk_state(paced.account_id).session_rest_until is None


@pytest.mark.parametrize("kind", [OperationKind.LOGIN, OperationKind.DOWNLOAD])
def test_login_and_media_download_do_not_increment_session(paced, kind):
    paced.risk.record_success(paced.account_id, kind)
    state = risk_state(paced.account_id)
    assert state.session_operation_count == 0
    assert state.operation_not_before is None and state.session_rest_until is None


def test_pacing_does_not_replace_stronger_write_limits(paced):
    now = datetime(2026, 9, 7, 2)
    paced.risk.record_success(paced.account_id, OperationKind.PUBLISH, now=now)
    decision = paced.risk.preflight(paced.account_id, OperationKind.PUBLISH, now=now)
    assert decision.signal == "kind_gap"
    assert decision.next_allowed_at >= now + timedelta(hours=2)
    # Even the internal interactive-read flag never relaxes a write limit.
    assert not paced.risk.preflight(paced.account_id, OperationKind.PUBLISH,
                                    now=now, interactive_read=True).allowed


@pytest.mark.parametrize("message", ["captcha required", "请完成人机验证", "安全验证", "security verification", "human verification"])
def test_challenge_requires_explicit_manual_clear_even_after_late_success(paced, message):
    now = datetime(2026, 9, 7, 2)
    paced.risk.record_failure(paced.account_id, OperationKind.READ_HEAVY, message, now=now)
    paced.risk.record_success(paced.account_id, OperationKind.READ_LIGHT, now=now + timedelta(hours=2))
    state = risk_state(paced.account_id)
    assert state.manual_review_required and state.recovery_successes == 0
    for kind in (OperationKind.READ_LIGHT, OperationKind.READ_HEAVY, OperationKind.COMMENT):
        decision = RiskController(paced.cfg).preflight(paced.account_id, kind, now=now + timedelta(days=1))
        assert decision.signal == "manual_review_required" and decision.next_allowed_at is None
    paced.risk.clear_account(paced.account_id, reason="Completed manual verification")
    assert not risk_state(paced.account_id).manual_review_required
    assert paced.risk.preflight(paced.account_id, OperationKind.READ_LIGHT, now=now + timedelta(days=1)).allowed


def test_rate_limit_without_challenge_keeps_gradual_recovery(paced):
    now = datetime(2026, 9, 7, 2)
    paced.risk.record_failure(paced.account_id, OperationKind.COMMENT, "HTTP 429", now=now)
    assert not risk_state(paced.account_id).manual_review_required
    assert paced.risk.preflight(paced.account_id, OperationKind.READ_LIGHT, now=now + timedelta(hours=1)).allowed
    assert paced.risk.preflight(paced.account_id, OperationKind.COMMENT, now=now + timedelta(hours=1)).signal == "probe_only"


def test_network_backoff_is_persisted_and_late_success_does_not_erase_it(paced):
    now = datetime(2026, 9, 7, 2)
    paced.risk.record_failure(paced.account_id, OperationKind.READ_HEAVY, TimeoutError(), now=now)
    deadline = risk_state(paced.account_id).retry_not_before
    assert now + timedelta(minutes=5) <= deadline <= now + timedelta(seconds=330)
    paced.risk.record_success(paced.account_id, OperationKind.READ_LIGHT, now=now + timedelta(seconds=1))
    assert risk_state(paced.account_id).retry_not_before == deadline
    assert RiskController(paced.cfg).preflight(paced.account_id, OperationKind.READ_LIGHT, now=now + timedelta(minutes=4)).signal == "network_backoff"
    assert paced.risk.preflight(paced.account_id, OperationKind.READ_LIGHT, now=deadline).allowed


@pytest.mark.parametrize("hold", ["manual_review_required", "retry_not_before", "session_rest_until", "cooldown_until"])
def test_interactive_reads_honor_hard_holds_without_opening_browser(paced, hold):
    with db.get_session() as session:
        state = AccountRiskState(account_id=paced.account_id)
        setattr(state, hold, True if hold == "manual_review_required" else datetime.utcnow() + timedelta(hours=1))
        session.add(state); session.commit()
    operation = AsyncMock(side_effect=AssertionError("blocked read was executed"))
    result, error = asyncio.run(paced.engine.guarded_interactive_read_pair(
        paced.account_id, OperationKind.READ_HEAVY, "fixture", operation, empty_result={}))
    assert result == {} and error.startswith("risk_deferred:")
    operation.assert_not_awaited()


def test_adjacent_interactive_reads_skip_only_normal_cadence(paced):
    paced.cfg.risk_control.read_heavy_gap_seconds = 60
    paced.risk.record_success(paced.account_id, OperationKind.READ_HEAVY)
    assert not paced.risk.preflight(paced.account_id, OperationKind.READ_HEAVY).allowed
    operation = AsyncMock(return_value=({"messages": []}, ""))
    result, error = asyncio.run(paced.engine.guarded_interactive_read_pair(
        paced.account_id, OperationKind.READ_HEAVY, "fixture", operation, empty_result={}))
    assert result == {"messages": []} and not error
    operation.assert_awaited_once()


def test_interactive_read_rechecks_risk_after_waiting_for_account_lock(paced):
    async def scenario():
        lock = paced.project.browser.lock_for(f"acc:{paced.account_id}")
        await lock.acquire()
        operation = AsyncMock(return_value=({}, ""))
        task = asyncio.create_task(paced.engine.guarded_interactive_read_pair(
            paced.account_id, OperationKind.READ_HEAVY, "fixture", operation, empty_result={}))
        await asyncio.sleep(0)
        paced.risk.record_failure(paced.account_id, OperationKind.READ_HEAVY, "captcha")
        lock.release()
        _, error = await task
        assert error.startswith("risk_deferred:")
        operation.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("creator_only,available", [(False, True), (True, True), (False, False)])
def test_background_health_respects_browser_read_mode(paced, monkeypatch, creator_only, available):
    browser = paced.project.browser
    if available:
        browser.visible_page = AsyncMock(side_effect=AssertionError("use profile stub"))
    profile = AsyncMock(return_value=({"user_id": "fixture", "nickname": "fixture"}, ""))
    monkeypatch.setattr(monitor, "fetch_xhs_self_profile", profile)
    direct = Mock(side_effect=AssertionError("browser mode must not fall back to direct API"))
    monkeypatch.setattr(paced.engine, "_xhs_client", direct)
    monkeypatch.setattr(monitor, "creator_check", direct)
    state = json.dumps({"cookies": [] if creator_only else [{"name": "web_session", "value": "fixture"}]})
    probe = (paced.account_id, "xhs", state, '{"cookies": []}', "", SimpleNamespace(timezone_id="Asia/Shanghai"))
    result = asyncio.run(paced.engine._probe_account_health(probe))
    if available and not creator_only:
        assert result["ok"]
        profile.assert_awaited_once()
    else:
        assert result["indeterminate"]
        profile.assert_not_awaited()
        with db.get_session() as session:
            assert session.get(DouyinAccount, paced.account_id).status == "active"
            assert not session.exec(select(RiskEvent)).all()
    direct.assert_not_called()


def test_health_probe_pauses_before_browser_or_network_when_challenged(paced, monkeypatch):
    paced.risk.record_failure(paced.account_id, OperationKind.READ_HEAVY, "captcha")
    profile = AsyncMock(side_effect=AssertionError("no platform traffic while challenged"))
    monkeypatch.setattr(monitor, "fetch_xhs_self_profile", profile)
    monkeypatch.setattr(paced.engine, "_verify_proxy_region", profile)
    result = asyncio.run(paced.engine._probe_account_health((
        paced.account_id, "xhs", "{}", "", "", SimpleNamespace(timezone_id="Asia/Shanghai"))))
    assert result["deferred"]
    profile.assert_not_awaited()


def test_runtime_pacing_settings_round_trip_and_invalid_ranges_are_atomic(paced):
    patch = {"operation_gap_min_seconds": 9, "operation_gap_max_seconds": 29,
             "session_operation_limit": 8, "session_rest_min_seconds": 240,
             "session_rest_max_seconds": 600}
    apply_risk_settings(paced.cfg, {"risk_control": patch})
    save_risk_settings(paced.cfg)
    restored = type(paced.cfg)()
    assert load_persisted_risk_settings(restored)
    for name, value in patch.items():
        assert getattr(restored.risk_control, name) == value
    before = export_risk_settings(paced.cfg)
    for invalid in ({"operation_gap_min_seconds": 30}, {"session_rest_min_seconds": 601},
                    {"session_operation_limit": -1}, {"session_rest_max_seconds": 0}):
        with pytest.raises(RiskSettingsError):
            apply_risk_settings(paced.cfg, {"risk_control": invalid})
        assert export_risk_settings(paced.cfg) == before


def test_risk_status_distinguishes_manual_hold_from_normal_rest(paced):
    paced.risk.record_failure(paced.account_id, OperationKind.READ_HEAVY, "captcha")
    accounts = asyncio.run(main.list_risk_control_accounts("xhs"))
    assert accounts[0]["status"] == "verification_required"
    assert accounts[0]["manual_review_required"] and accounts[0]["next_probe_at"] is None
    paced.risk.clear_account(paced.account_id)
    with db.get_session() as session:
        state = session.get(AccountRiskState, paced.account_id)
        state.session_rest_until = datetime.utcnow() + timedelta(minutes=3)
        session.add(state); session.commit()
    accounts = asyncio.run(main.list_risk_control_accounts("xhs"))
    assert accounts[0]["status"] == "normal" and accounts[0]["session_rest_until"]
