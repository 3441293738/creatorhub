"""Offline configuration management: atomic patches, persistence and allowlisting."""
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlmodel import Session

import app.db as db
import app.main as main
from app.config import Config, EngineConfig
from app.engine.monitor import MonitorEngine
from app.engine_settings import (
    ENGINE_SETTINGS_KEY, EngineSettingsPatch, export_engine_settings,
    load_persisted_engine_settings, save_engine_settings,
)
from app.models import AppSetting, CommentWatch, DouyinAccount, MonitorTarget
from test_project_optimizations import local_project, store


def request(method="GET", payload=None, *, headers=None, peer="127.0.0.1"):
    async def run():
        transport = httpx.ASGITransport(app=main.app, client=(peer, 1234), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            kwargs = {"headers": headers or {}}
            if payload is not None:
                kwargs["content"] = json.dumps(payload, allow_nan=True)
                kwargs["headers"] = {"Content-Type": "application/json", **kwargs["headers"]}
            return await client.request(method, "/api/settings/engine", **kwargs)
    return asyncio.run(run())


def saved():
    with db.get_session() as session:
        row = session.get(AppSetting, ENGINE_SETTINGS_KEY)
        return json.loads(row.value) if row else None


def test_settings_export_only_the_supported_fields(local_project):
    cfg = local_project.cfg
    cfg.engine.xhs_read_mode = "api"
    cfg.proxies = ["private-proxy-fixture"]
    response = request()
    assert response.status_code == 200
    body = response.json()
    keys = set(EngineSettingsPatch.model_fields)
    assert set(body["values"]) == set(body["defaults"]) == keys
    assert body["values"]["xhs_read_mode"] == "api"
    assert body["defaults"] == {key: getattr(EngineConfig(), key) for key in keys}
    assert body["saved_fields"] == [] and body["apply_scope"] == "next_operation"
    assert "private-proxy-fixture" not in response.text
    assert "profiles_dir" not in response.text and "user_agent" not in response.text
    assert saved() is None
    assert response.headers["cache-control"] == "no-store"


def test_partial_save_persists_without_changing_the_base_file(local_project):
    cfg = local_project.cfg
    config_file = local_project.root / "base.yaml"
    config_file.write_text("engine:\n  request_timeout_seconds: 30\n", encoding="utf-8")
    original = config_file.read_bytes()
    cfg.engine.request_timeout_seconds = 30
    cfg.engine.scan_concurrency = 3
    assert request("PUT", {"xhs_read_mode": "api", "comment_recent_days": 10}).status_code == 200
    result = request("PUT", {"download_timeout_seconds": 240})
    assert result.status_code == 200
    expected = {"xhs_read_mode": "api", "comment_recent_days": 10, "download_timeout_seconds": 240}
    assert saved() == expected
    assert result.json()["saved_fields"] == sorted(expected)
    assert cfg.engine.request_timeout_seconds == 30 and cfg.engine.scan_concurrency == 3
    assert config_file.read_bytes() == original

    restarted = Config()
    restarted.engine.request_timeout_seconds = 35  # A later base-file edit still wins for untouched fields.
    restarted.engine.xhs_read_mode = "browser"
    assert load_persisted_engine_settings(restarted)
    for key, value in expected.items():
        assert getattr(restarted.engine, key) == value
    assert restarted.engine.request_timeout_seconds == 35


def test_all_supported_values_and_live_downloader(local_project, monkeypatch):
    cfg = local_project.cfg
    engine = MonitorEngine(cfg, local_project.browser)
    monkeypatch.setattr(main, "engine", engine)
    payload = {
        "xhs_read_mode": "api", "monitor_initial_backfill_count": -1,
        "comment_recent_works": 8, "comment_recent_days": 10, "comment_max_scrolls": 9,
        "request_timeout_seconds": 30, "download_timeout_seconds": 240,
        "xhs_item_gap_seconds": 4.5, "xhs_request_jitter": .5,
        "xhs_publish_mode": "api", "xhs_comment_write_mode": "manual",
        "xhs_comment_review_before_publish": False, "work_health_enabled": True,
        "work_health_interval_seconds": 7200, "work_health_zero_play_hours": 12.5,
        "work_health_recent_days": 14, "work_health_stat_snapshots": False,
    }
    assert set(payload) == set(EngineSettingsPatch.model_fields)
    response = request("PUT", payload)
    assert response.status_code == 200, response.text
    assert response.json()["values"] == payload and saved() == payload
    assert engine.downloader.timeout == 240
    assert engine.cfg is cfg and not engine._xhs_browser_reads_enabled()
    reset = request("PUT", response.json()["defaults"])
    assert reset.status_code == 200
    assert cfg.engine.xhs_read_mode == "browser" and cfg.engine.xhs_comment_review_before_publish
    assert not cfg.engine.work_health_enabled


INVALID = [
    {"host": "0.0.0.0"}, {"profiles_dir": "arbitrary-directory"}, {"ai_api_key": "do-not-echo-fixture"},
    {"xhs_read_mode": "unknown"}, {"xhs_publish_mode": "API"}, {"xhs_comment_write_mode": "auto"},
    {"monitor_initial_backfill_count": -2}, {"monitor_initial_backfill_count": 1001},
    {"comment_recent_works": 0}, {"comment_recent_works": True}, {"comment_recent_works": "5"},
    {"comment_recent_works": 1.5}, {"comment_recent_works": 101},
    {"comment_recent_days": 366}, {"comment_max_scrolls": 31}, {"request_timeout_seconds": 4},
    {"download_timeout_seconds": 1801}, {"xhs_request_jitter": 1.01}, {"xhs_item_gap_seconds": -1},
    {"xhs_request_jitter": float("nan")}, {"xhs_item_gap_seconds": float("inf")},
    {"xhs_item_gap_seconds": float("-inf")}, {"xhs_item_gap_seconds": True},
    {"work_health_enabled": 1}, {"work_health_enabled": "false"},
    {"work_health_interval_seconds": 599}, {"work_health_zero_play_hours": 169},
    {"work_health_recent_days": 0}, {"work_health_stat_snapshots": None},
    {"xhs_comment_review_before_publish": "true"}, {"xhs_read_mode": None},
]


@pytest.mark.parametrize("invalid", INVALID)
def test_invalid_patch_is_atomic_and_returns_a_field_error(local_project, invalid):
    save_engine_settings(local_project.cfg, {"comment_recent_days": 9})
    before = asdict(local_project.cfg)
    response = request("PUT", {"comment_recent_days": 11, **invalid})
    assert response.status_code == 422, response.text
    assert asdict(local_project.cfg) == before
    assert saved() == {"comment_recent_days": 9}
    details = response.json()["detail"]
    assert isinstance(details, list)
    assert any(item["loc"][-1] == next(iter(invalid)) for item in details)
    assert all("input" not in item for item in details)
    assert "do-not-echo-fixture" not in response.text


@pytest.mark.parametrize("payload", [[], "text", False, 123, float("nan")])
def test_non_object_payloads_are_json_validation_errors(local_project, payload):
    response = request("PUT", payload)
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], list)
    assert saved() is None


def test_failed_commit_keeps_live_and_saved_config_unchanged(local_project, monkeypatch):
    cfg = local_project.cfg
    save_engine_settings(cfg, {"xhs_read_mode": "api"})
    downloader = SimpleNamespace(timeout=120)
    main.engine.downloader = downloader
    before = asdict(cfg)

    def fail(_session):
        raise OSError("private-database-location-fixture")

    with monkeypatch.context() as scoped:
        scoped.setattr(Session, "commit", fail)
        response = request("PUT", {"xhs_read_mode": "browser", "download_timeout_seconds": 300})
    assert response.status_code == 503
    assert "private-database-location-fixture" not in response.text
    assert asdict(cfg) == before and downloader.timeout == 120
    assert saved() == {"xhs_read_mode": "api"}


@pytest.mark.parametrize("blob", ["{broken", "[]", "null", '{"xhs_read_mode":"api","host":"bad"}',
    '{"xhs_read_mode":"api","comment_recent_days":null}', '{"xhs_request_jitter":NaN}'])
def test_corrupt_persisted_settings_do_not_partially_override_startup(local_project, blob):
    with db.get_session() as session:
        session.add(AppSetting(key=ENGINE_SETTINGS_KEY, value=blob))
        session.commit()
    before = asdict(local_project.cfg)
    assert not load_persisted_engine_settings(local_project.cfg)
    assert asdict(local_project.cfg) == before
    assert export_engine_settings(local_project.cfg)["saved_fields"] == []
    # A valid save can repair the invalid override record.
    assert request("PUT", {"comment_recent_days": 10}).status_code == 200
    assert saved() == {"comment_recent_days": 10}


def test_noop_save_does_not_pin_file_defaults(local_project):
    assert request("PUT", {}).status_code == 200
    assert saved() is None


@pytest.mark.parametrize("override,expected", [(None, 12), (0, 0), (-1, -1)])
def test_new_monitors_inherit_defaults_without_changing_existing_ones(local_project, monkeypatch, override, expected):
    account_id = store(DouyinAccount(status="active"))
    old_id = store(MonitorTarget(account_id=account_id, sec_uid="old-fixture", initial_backfill_count=5))
    monkeypatch.setattr(main, "resolve_sec_uid", AsyncMock(return_value="new-fixture"))
    assert request("PUT", {"monitor_initial_backfill_count": 12}).status_code == 200
    result = asyncio.run(main.add_monitor(main.TargetIn(
        url_or_secuid="fixture", account_id=account_id, initial_backfill_count=override)))
    assert result["initial_backfill_count"] == expected
    assert request("PUT", {"monitor_initial_backfill_count": 20}).status_code == 200
    with db.get_session() as session:
        assert session.get(MonitorTarget, result["id"]).initial_backfill_count == expected
        assert session.get(MonitorTarget, old_id).initial_backfill_count == 5


def test_comment_defaults_hot_apply_only_to_inheriting_watches(local_project):
    inherited = store(CommentWatch(recent_works=0, recent_days=0, max_scrolls=0))
    custom = store(CommentWatch(recent_works=3, recent_days=5, max_scrolls=4))
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    assert request("PUT", {"comment_recent_works": 8, "comment_recent_days": 14, "comment_max_scrolls": 10}).status_code == 200
    assert engine._comment_watch_settings(inherited) == {"recent_works": 8, "recent_days": 14, "max_scrolls": 10}
    assert engine._comment_watch_settings(custom) == {"recent_works": 3, "recent_days": 5, "max_scrolls": 4}


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_engine_settings_keep_local_and_same_origin_boundaries(local_project, method):
    payload = {"xhs_read_mode": "api"} if method == "PUT" else None
    assert request(method, payload, peer="192.0.2.1").status_code == 403
    assert request(method, payload, headers={"Origin": "https://fixture.invalid"}).status_code == 403
    assert saved() is None


def test_overrides_are_loaded_before_the_engine_is_constructed():
    source = Path(main.__file__).read_text(encoding="utf-8")
    startup = source.split("async def lifespan", 1)[1].split("yield", 1)[0]
    assert startup.index("load_persisted_engine_settings(cfg)") < startup.index("engine = MonitorEngine(cfg, browser)")
