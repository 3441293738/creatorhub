"""Relay selections preserve user intent and durable submission semantics."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from sqlmodel import select

import app.db as db
import app.main as main
from app.engine.monitor import MonitorEngine
from app.models import ContentRecord, DouyinAccount, PublishTask, TaskSubmission
from test_project_optimizations import local_project, store


@pytest.fixture
def relay(local_project, monkeypatch):
    paths = []
    for index in range(3):
        path = local_project.root / f"fixture_title_{index}.jpg"
        path.write_bytes(str(index).encode())
        paths.append(str(path))
    content = store(ContentRecord(
        platform="douyin", target_id=0, aweme_id="fixture", media_type="images",
        download_status="done", local_path=str(local_project.root)))
    engine = MonitorEngine.__new__(MonitorEngine)
    monkeypatch.setattr(main, "engine", engine)
    return engine, content, paths


@pytest.mark.parametrize("platform", ["xhs", "douyin", "shipinhao"])
@pytest.mark.parametrize("order", [[], [99], [-1], [0, 99], [0, 0], [True],
                                   [1.0], ["1"], [None], [[0]], "0", (0,)])
def test_invalid_relay_selection_never_creates_a_task(relay, platform, order):
    engine, content, _ = relay
    with pytest.raises(ValueError, match="转发媒体选择"):
        engine.create_relay_publish(content, 1, target_platform=platform, media_order=order)
    with db.get_session() as session:
        assert not session.exec(select(PublishTask)).all()


@pytest.mark.parametrize("order,indices", [(None, [0, 1, 2]), ([2, 0], [2, 0]), ([1], [1])])
def test_valid_relay_keeps_exact_selection_and_cover(relay, order, indices):
    engine, content, paths = relay
    task_id = engine.create_relay_publish(content, 1, media_order=order)
    with db.get_session() as session:
        task = session.get(PublishTask, task_id)
        assert json.loads(task.media_json) == [paths[i] for i in indices]


@pytest.mark.parametrize("order", [[], [99], [0, 0], [True], [1.0], ["1"]])
def test_relay_api_rejects_invalid_selection_without_receipt(relay, order):
    _, content, _ = relay
    account = store(DouyinAccount(platform="xhs", creator_storage_state="{}"))
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://127.0.0.1") as client:
            headers = {"Idempotency-Key": "relay-fixture-submission"}
            response = await client.post(f"/api/contents/{content}/repost-xhs",
                json={"account_id": account, "media_order": order}, headers=headers)
            assert response.status_code == 422, response.text
            assert "转发媒体选择" in response.text or "media_order" in response.text
            with db.get_session() as session:
                assert not session.exec(select(PublishTask)).all()
                assert not session.exec(select(TaskSubmission)).all()
            # A corrected selection may reuse the rejected submission's key.
            body = {"account_id": account, "media_order": [2, 0]}
            response = await client.post(f"/api/contents/{content}/repost-xhs", json=body, headers=headers)
            assert response.status_code == 200, response.text
            task_id = response.json()["task_id"]
            for path in relay[2]:
                Path(path).unlink()
            replay = await client.post(f"/api/contents/{content}/repost-xhs", json=body, headers=headers)
            assert replay.status_code == 200 and replay.json()["replayed"]
            assert replay.json()["task_id"] == task_id
    asyncio.run(scenario())
