"""Account-scoped homepage monitors, using temporary SQLite and local ASGI."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlmodel import select

import app.db as db
import app.engine.monitor as monitor
import app.main as main
from app.engine.monitor import MonitorEngine
from app.models import ContentRecord, DouyinAccount, MonitorTarget
from test_project_optimizations import local_project, store


@pytest.fixture(params=["douyin", "xhs", "kuaishou"])
def homepage(local_project, monkeypatch, request):
    platform = request.param
    accounts = [store(DouyinAccount(platform=platform, status="active"))
                for _ in range(2)]
    resolved = SimpleNamespace(user_id="TARGET", xsec_token="TOKEN")
    for name, value in (("resolve_sec_uid", "TARGET"),
                        ("xhs_resolve_user", resolved),
                        ("resolve_ks_user_id", "TARGET")):
        monkeypatch.setattr(main, name, AsyncMock(return_value=value))
    return SimpleNamespace(platform=platform, accounts=accounts)


def request(method, path="/api/monitors", **kwargs):
    async def run():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://127.0.0.1") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


def create(homepage, account_id, **kwargs):
    return request("POST", json={
        "platform": homepage.platform, "url_or_secuid": "TARGET",
        "account_id": account_id, "download_enabled": False, **kwargs,
    })


def test_different_accounts_can_monitor_the_same_resolved_homepage(homepage):
    first = create(homepage, homepage.accounts[0], alias="任务 A", interval_seconds=300)
    second = create(homepage, homepage.accounts[1], alias="任务 B", interval_seconds=600)
    assert first.status_code == second.status_code == 200
    a, b = first.json(), second.json()
    assert a["id"] != b["id"] and a["sec_uid"] == b["sec_uid"] == "TARGET"
    rows = request("GET", params={"platform": homepage.platform}).json()
    assert {(row["account_id"], row["alias"], row["interval_seconds"])
            for row in rows} == {
        (homepage.accounts[0], "任务 A", 300),
        (homepage.accounts[1], "任务 B", 600),
    }


@pytest.mark.parametrize("enabled", [True, False])
def test_same_account_duplicate_is_rejected_even_when_paused(homepage, enabled):
    first = create(homepage, homepage.accounts[0]).json()
    if not enabled:
        assert request("POST", f"/api/monitors/{first['id']}/toggle").status_code == 200
    # Different input text resolving to the same homepage must still conflict.
    duplicate = create(homepage, homepage.accounts[0], url_or_secuid="TARGET_ALIAS")
    assert duplicate.status_code == 409
    assert "账号" in duplicate.json()["detail"]
    with db.get_session() as session:
        rows = session.exec(select(MonitorTarget)).all()
        assert len(rows) == 1 and rows[0].enabled == enabled
    assert create(homepage, homepage.accounts[1]).status_code == 200


def test_concurrent_creations_keep_duplicates_scoped_to_each_account(homepage):
    async def run():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=main.app),
                base_url="http://127.0.0.1") as client:
            return await asyncio.gather(*(client.post("/api/monitors", json={
                "platform": homepage.platform, "url_or_secuid": "TARGET",
                "account_id": account,
            }) for account in (homepage.accounts[0], *homepage.accounts)))

    results = asyncio.run(run())
    assert sorted(result.status_code for result in results) == [200, 200, 409]
    with db.get_session() as session:
        rows = session.exec(select(MonitorTarget)).all()
        assert len(rows) == 2
        assert {row.account_id for row in rows} == set(homepage.accounts)


@pytest.mark.parametrize("enabled", [True, False])
def test_switching_to_an_occupied_account_rolls_back_all_edits(homepage, enabled):
    a = store(MonitorTarget(platform=homepage.platform, sec_uid="TARGET",
        account_id=homepage.accounts[0], alias="原任务", interval_seconds=300))
    b = store(MonitorTarget(platform=homepage.platform, sec_uid="TARGET",
        account_id=homepage.accounts[1], enabled=enabled))
    result = request("PUT", f"/api/monitors/{a}", json={
        "account_id": homepage.accounts[1], "alias": "不应保存",
        "interval_seconds": 1200, "download_enabled": False,
    })
    assert result.status_code == 409
    with db.get_session() as session:
        row = session.get(MonitorTarget, a)
        assert (row.account_id, row.alias, row.interval_seconds,
                row.download_enabled) == (homepage.accounts[0], "原任务", 300, True)
        assert session.get(MonitorTarget, b).enabled == enabled


def test_edit_self_and_switch_to_free_account_preserve_history(homepage):
    scanned = datetime(2026, 9, 1)
    target = store(MonitorTarget(platform=homepage.platform, sec_uid="TARGET",
        account_id=homepage.accounts[0], last_scan_at=scanned))
    store(MonitorTarget(platform=homepage.platform, sec_uid="OTHER_TARGET",
        account_id=homepage.accounts[1]))
    record = store(ContentRecord(platform=homepage.platform, target_id=target,
        aweme_id="shared-work", download_status="skipped"))
    for account in homepage.accounts[:2]:
        result = request("PUT", f"/api/monitors/{target}", json={
            "account_id": account, "alias": "更新备注",
        })
        assert result.status_code == 200 and result.json()["account_id"] == account
    with db.get_session() as session:
        assert session.get(MonitorTarget, target).last_scan_at == scanned
        assert session.get(ContentRecord, record).target_id == target
    assert create(homepage, homepage.accounts[0]).status_code == 200


def test_removing_one_monitor_preserves_the_other_and_old_records(homepage):
    a = create(homepage, homepage.accounts[0]).json()
    response = create(homepage, homepage.accounts[1])
    assert response.status_code == 200
    b = response.json()
    for target in (a, b):
        store(ContentRecord(platform=homepage.platform, target_id=target["id"],
            aweme_id="shared-work", download_status="skipped"))
    assert request("DELETE", f"/api/monitors/{a['id']}").status_code == 200
    new = create(homepage, homepage.accounts[0]).json()
    assert new["id"] not in (a["id"], b["id"])
    rows = request("GET", params={"platform": homepage.platform}).json()
    assert {row["id"]: row["content_count"] for row in rows} == {new["id"]: 0, b["id"]: 1}
    old_records = request("GET", f"/api/monitors/{a['id']}/contents").json()
    assert len(old_records) == 1 and old_records[0]["source"]["deleted"]


@pytest.mark.parametrize("legacy_account", [None, 0])
def test_anonymous_homepage_has_its_own_duplicate_scope(homepage, legacy_account):
    if homepage.platform == "douyin":
        assert create(homepage, None).status_code == 400
        return
    store(MonitorTarget(platform=homepage.platform, sec_uid="TARGET", account_id=legacy_account))
    for account in (None, 0):
        assert create(homepage, account).status_code == 409
    assert create(homepage, homepage.accounts[0]).status_code == 200


def test_keyword_monitors_keep_the_existing_global_duplicate_rule(local_project):
    accounts = [store(DouyinAccount(platform="xhs", status="active")) for _ in range(2)]
    body = {"platform": "xhs", "target_kind": "keyword", "url_or_secuid": "同一关键词"}
    assert request("POST", json={**body, "account_id": accounts[0]}).status_code == 200
    assert request("POST", json={**body, "account_id": accounts[1]}).status_code == 409


@pytest.mark.parametrize("account_kind", ["missing", "expired", "wrong_platform"])
def test_douyin_account_validation_is_preserved(local_project, monkeypatch, account_kind):
    monkeypatch.setattr(main, "resolve_sec_uid", AsyncMock(return_value="TARGET"))
    account = 999 if account_kind == "missing" else store(DouyinAccount(
        platform="xhs" if account_kind == "wrong_platform" else "douyin",
        status="expired" if account_kind == "expired" else "active"))
    result = request("POST", json={"url_or_secuid": "TARGET", "account_id": account})
    assert result.status_code == 400
    with db.get_session() as session:
        assert not session.exec(select(MonitorTarget)).all()


def test_douyin_scans_keep_account_identity_and_content_dedup_separate(local_project, monkeypatch):
    local_project.cfg.risk_control.enabled = False
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    accounts = [store(DouyinAccount(status="active")) for _ in range(2)]
    monkeypatch.setattr(engine, "_identity_proxy", lambda acc: (acc.id, ""))
    before = datetime.utcnow() - timedelta(hours=1)
    targets = [store(MonitorTarget(sec_uid="TARGET", account_id=account,
        created_at=before, last_scan_at=before, download_enabled=False)) for account in accounts]
    seen = []

    async def fetch(_browser, identity, sec_uid, known, **_kwargs):
        seen.append((identity, sec_uid, set(known)))
        await asyncio.sleep(0)  # Both account scans may be in flight together.
        items = [] if "shared-work" in known else [{
            "aweme_id": "shared-work", "desc": "同一作品",
            "create_time": int(datetime.utcnow().timestamp()),
            "video": {"play_addr": {"url_list": ["https://media.invalid/fixture.mp4"]}},
        }]
        return items, {"nickname": "主页"}, ""

    monkeypatch.setattr(monitor, "fetch_videos", fetch)
    engine._notify_new = AsyncMock()
    engine._download = AsyncMock()

    async def scan_both():
        return await asyncio.gather(*(engine.scan_target(target) for target in targets))

    assert all(row["ok"] and row["new"] == 1 for row in asyncio.run(scan_both()))
    assert {(account, uid) for account, uid, known in seen if not known} == {
        (account, "TARGET") for account in accounts}
    with db.get_session() as session:
        records = session.exec(select(ContentRecord)).all()
        assert {(row.target_id, row.aweme_id) for row in records} == {
            (target, "shared-work") for target in targets}
    assert all(row["ok"] and row["new"] == 0 for row in asyncio.run(scan_both()))
    assert all(known == {"shared-work"} for _, _, known in seen[2:])
    assert engine._notify_new.await_count == 2
    engine._download.assert_not_awaited()
