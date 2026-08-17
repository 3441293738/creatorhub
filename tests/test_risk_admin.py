import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx

import app.db as db
import app.main as main
from starlette.requests import Request
from sqlmodel import select
from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import (AccountRiskState, AppSetting, DouyinAccount, RiskEvent,
                        RiskAdminAudit, KeywordCollectionJob, PublishTask)
from app.risk_admin import (
    RISK_SETTINGS_KEY,
    RiskSettingsError,
    apply_risk_settings,
    export_risk_settings,
    load_persisted_risk_settings,
    save_risk_settings,
)


class _Identity:
    timezone_id = "Asia/Shanghai"


class _BrowserStub:
    def __init__(self):
        import asyncio
        self.locks = {}

    def lock_for(self, key):
        import asyncio
        return self.locks.setdefault(key, asyncio.Lock())

    def identity_for(self, _account):
        return _Identity()


class RiskAdminTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.previous_cfg = main.cfg
        self.previous_main_engine = main.engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "risk-admin.db"))
        self.cfg = Config()
        main.cfg = self.cfg
        main.engine = None

    def tearDown(self):
        main.cfg = self.previous_cfg
        main.engine = self.previous_main_engine
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def test_apply_and_persist_runtime_rules(self):
        payload = export_risk_settings(self.cfg)
        payload["risk_control"].update({
            "mode": "custom",
            "read_heavy_gap_seconds": 125,
            "cooldown_steps_seconds": [60, 300, 900],
            "recovery_successes": 4,
        })
        payload["schedule"].update({
            "quiet_hours_enabled": True,
            "active_hours_start": 9,
            "active_hours_end": 23,
        })

        apply_risk_settings(self.cfg, payload)
        save_risk_settings(self.cfg)

        restored = Config()
        self.assertTrue(load_persisted_risk_settings(restored))
        self.assertEqual(restored.risk_control.mode, "custom")
        self.assertEqual(restored.risk_control.read_heavy_gap_seconds, 125)
        self.assertEqual(restored.risk_control.cooldown_steps_seconds, [60, 300, 900])
        self.assertEqual(restored.engine.active_hours_start, 9)
        with db.get_session() as session:
            stored = session.get(AppSetting, RISK_SETTINGS_KEY)
            self.assertEqual(json.loads(stored.value)["risk_control"]["recovery_successes"], 4)

    def test_invalid_update_is_atomic(self):
        before = export_risk_settings(self.cfg)
        payload = export_risk_settings(self.cfg)
        payload["risk_control"]["read_heavy_gap_seconds"] = 999
        payload["risk_control"]["cooldown_steps_seconds"] = [600, 60]

        with self.assertRaises(RiskSettingsError):
            apply_risk_settings(self.cfg, payload)

        self.assertEqual(export_risk_settings(self.cfg), before)

    def test_account_view_exposes_reason_recovery_and_queued_state(self):
        now = datetime.utcnow()
        with db.get_session() as session:
            account = DouyinAccount(platform="douyin", nickname="fixture", status="active",
                                    douyin_id="actual-account-9527")
            session.add(account)
            session.commit()
            session.refresh(account)
            session.add(AccountRiskState(
                account_id=account.id,
                risk_level=1,
                cooldown_until=now + timedelta(minutes=30),
                recovery_successes=0,
                last_risk_at=now,
                last_risk_reason="触发验证码",
            ))
            session.add(RiskEvent(
                account_id=account.id,
                operation_kind="read_heavy",
                outcome="risk",
                signal="platform_risk",
                detail="触发验证码",
                occurred_at=now,
            ))
            session.add(KeywordCollectionJob(
                platform="douyin", account_id=account.id, status="pending",
                current_step="等待账号读取冷却", blocked_reason="触发验证码",
                blocked_signal="cooldown", blocked_operation="read_heavy",
                blocked_at=now, next_allowed_at=now + timedelta(minutes=30),
            ))
            session.commit()
            account_id = account.id

        rows = asyncio.run(main.list_risk_control_accounts("douyin"))

        row = next(item for item in rows if item["account_id"] == account_id)
        self.assertEqual(row["status"], "cooldown")
        self.assertEqual(row["reason"], "触发验证码")
        self.assertEqual(row["last_operation_kind"], "read_heavy")
        self.assertEqual(row["platform_account_id"], "actual-account-9527")
        self.assertEqual(row["platform_account_id_label"], "抖音号")
        self.assertGreater(row["cooldown_remaining_seconds"], 0)
        self.assertEqual(row["blocked_tasks"], 1)
        self.assertEqual(row["blocked_signals"], {"cooldown": 1})

    def test_config_endpoint_applies_policy_immediately(self):
        payload = export_risk_settings(self.cfg)
        payload["risk_control"]["recovery_successes"] = 5
        body = main.RiskSettingsIn(**payload)

        request = Request({"type": "http", "headers": [],
                           "client": ("127.0.0.1", 1234)})
        result = asyncio.run(main.put_risk_control_config(body, request))

        self.assertEqual(result["risk_control"]["recovery_successes"], 5)
        self.assertEqual(self.cfg.risk_control.recovery_successes, 5)

    def test_deferred_task_persists_exact_gate_metadata(self):
        engine = MonitorEngine(self.cfg, _BrowserStub())
        task = PublishTask(account_id=7, status="pending")
        next_at = datetime.utcnow() + timedelta(minutes=20)

        engine._defer_row(task, "账号处于平台风险冷却期", next_at,
                          signal="cooldown")

        self.assertEqual(task.blocked_reason, "账号处于平台风险冷却期")
        self.assertEqual(task.blocked_signal, "cooldown")
        self.assertEqual(task.blocked_operation, "publish")
        self.assertEqual(task.next_allowed_at, next_at)

    def test_recovery_scheduler_probes_and_wakes_collection_job(self):
        self.cfg.risk_control.recovery_successes = 1
        self.cfg.risk_control.recovery_probe_gap_seconds = 1
        self.cfg.engine.verify_proxy_region = False
        self.cfg.engine.work_health_stat_snapshots = False
        now = datetime.utcnow()
        with db.get_session() as session:
            account = DouyinAccount(
                platform="xhs", nickname="fixture", status="active",
                creator_storage_state='{"cookies": [{"name": "a1", "value": "x"}]}',
            )
            session.add(account); session.commit(); session.refresh(account)
            account_id = account.id
            session.add(AccountRiskState(
                account_id=account_id, risk_level=1,
                cooldown_until=now - timedelta(seconds=2),
                probe_only_until=now - timedelta(seconds=2),
                last_operation_at=now - timedelta(minutes=10),
                last_risk_reason="触发验证码",
            ))
            session.add(KeywordCollectionJob(
                platform="xhs", account_id=account_id, status="pending",
                current_step="等待账号读取冷却", blocked_reason="风险冷却",
                blocked_signal="cooldown", blocked_operation="read_heavy",
                blocked_at=now - timedelta(minutes=1), next_allowed_at=now,
            ))
            session.commit()

        engine = MonitorEngine(self.cfg, _BrowserStub())
        with patch("app.engine.monitor.creator_check", new=AsyncMock(return_value=True)):
            completed = asyncio.run(engine._process_risk_recovery())

        self.assertEqual(completed, 1)
        with db.get_session() as session:
            state = session.get(AccountRiskState, account_id)
            saved_job = session.exec(select(KeywordCollectionJob)).first()
            self.assertEqual(state.risk_level, 0)
            self.assertEqual(saved_job.blocked_reason, "")
            self.assertEqual(saved_job.current_step, "账号已恢复，等待继续")

    def test_manual_clear_requires_reason_and_records_audit(self):
        now = datetime.utcnow()
        with db.get_session() as session:
            account = DouyinAccount(platform="douyin", nickname="fixture")
            session.add(account); session.commit(); session.refresh(account)
            account_id = account.id
            session.add(AccountRiskState(
                account_id=account_id, risk_level=1,
                cooldown_until=now + timedelta(minutes=30),
                last_risk_reason="验证码"))
            session.commit()
        request = Request({"type": "http", "headers": [],
                           "client": ("127.0.0.1", 1234)})

        result = asyncio.run(main.clear_account_risk(
            account_id, main.RiskClearIn(
                confirmed=True, reason="已完成验证码并检查网络"), request))

        self.assertTrue(result["ok"])
        with db.get_session() as session:
            audit = session.exec(select(RiskAdminAudit)).first()
            event = session.exec(select(RiskEvent).order_by(
                RiskEvent.id.desc())).first()
            self.assertEqual(audit.action, "account_risk_cleared")
            self.assertIn("已完成验证码", event.detail)

    def test_optional_admin_token_protects_mutations(self):
        missing = Request({"type": "http", "headers": [],
                           "client": ("10.0.0.8", 1234)})
        allowed = Request({
            "type": "http",
            "headers": [(b"x-creatorhub-admin-token", b"fixture-secret")],
            "client": ("10.0.0.8", 1234),
        })
        with patch.dict(os.environ, {"CREATORHUB_ADMIN_TOKEN": "fixture-secret"}):
            with self.assertRaises(main.HTTPException) as caught:
                main._require_risk_admin(missing)
            self.assertEqual(caught.exception.status_code, 403)
            self.assertEqual(main._require_risk_admin(allowed), "local-ui@10.0.0.8")

    def test_risk_admin_http_routes(self):
        with db.get_session() as session:
            account = DouyinAccount(platform="douyin", nickname="http-fixture")
            session.add(account); session.commit(); session.refresh(account)
            account_id = account.id
            session.add(AccountRiskState(
                account_id=account_id, risk_level=1,
                cooldown_until=datetime.utcnow() + timedelta(minutes=5),
                last_risk_reason="安全验证"))
            session.commit()

        async def scenario():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                    transport=transport, base_url="http://test") as client:
                config = (await client.get("/api/risk-control/config")).json()
                config["risk_control"]["recovery_successes"] = 6
                updated = await client.put("/api/risk-control/config", json={
                    "risk_control": config["risk_control"],
                    "schedule": config["schedule"],
                })
                self.assertEqual(updated.status_code, 200)
                rows = (await client.get(
                    "/api/risk-control/accounts?platform=douyin")).json()
                self.assertEqual(rows[0]["status"], "cooldown")
                cleared = await client.post(
                    f"/api/risk-control/accounts/{account_id}/clear",
                    json={"confirmed": True, "reason": "已完成人工安全检查"})
                self.assertEqual(cleared.status_code, 200)
                audit = (await client.get("/api/risk-control/audit")).json()
                self.assertGreaterEqual(len(audit), 2)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
