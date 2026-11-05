"""本机 AI 二创视频计划、网关与渲染回归。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

import app.content_api as content_api
import app.main as main
from app.content_pipeline import ContentIndexStore, PipelineError
from app.content_video_pipeline import (
    AdsMediaGatewayClient,
    LocalVideoRenderer,
    VideoPlanService,
    estimate_plan_cost,
)


def _plan() -> dict:
    """返回一份 20 秒安全二创计划。"""
    return {
        "title": "进气压力传感器故障排查",
        "hook": "发动机抖动，不一定是点火系统。",
        "rights_mode": "regenerate",
        "scenes": [
            {
                "duration": 5,
                "visual_prompt": f"全新生成的竖屏汽修科普动画镜头 {index}",
                "narration": f"这是第{index}段重新创作的旁白。",
                "subtitle": f"第{index}段故障排查",
            }
            for index in range(1, 5)
        ],
    }


def _quote() -> dict:
    """返回一份服务端预览报价。"""
    return {
        "quote_id": "a" * 64,
        "quality": "preview",
        "model": "wanx2.1-t2v-turbo",
        "scene_count": 4,
        "scene_duration_seconds": 5,
        "total_cost_cny": 4.8,
    }


class FakeJSONCaller:
    """返回固定视频计划的文字模型替身。"""

    def call_json(self, prompt: str) -> tuple[dict, str]:
        """返回测试计划。"""
        assert "不得复用原视频画面" in prompt
        return _plan(), "deepseek"


def test_video_plan_requires_four_regenerated_scenes():
    """计划必须是四段全新画面，不能把原片包装成二创。"""
    service = VideoPlanService(FakeJSONCaller())
    plan, provider = service.create(
        {"title": "传感器故障", "summary": "清洗后恢复正常"},
        "原口播只用来提炼事实。",
    )
    assert provider == "deepseek"
    assert plan["rights_mode"] == "regenerate"
    assert len(plan["scenes"]) == 4
    assert sum(scene["duration"] for scene in plan["scenes"]) == 20
    assert estimate_plan_cost(plan, "preview") == 4.8


def test_video_plan_rejects_source_footage_reuse():
    """任何复用、裁剪或上传原片的计划都必须被门禁拒绝。"""
    unsafe = _plan()
    unsafe["rights_mode"] = "reuse_source"
    with pytest.raises(PipelineError, match="重新生成"):
        VideoPlanService.guard_plan(unsafe)


def test_media_gateway_never_sends_local_source_path():
    """客户端只能发送生成提示词，不能发送原片路径或口播全文。"""
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url.path == "/login":
            return httpx.Response(
                302,
                headers={"set-cookie": "session_token=test; Path=/; HttpOnly; Secure"},
            )
        if request.url.path == "/api/content-index/video/submit":
            body = json.loads(request.content)
            assert set(body) == {
                "prompt",
                "duration",
                "quality",
                "cost_confirmed",
                "quote_id",
            }
            assert body["cost_confirmed"] is True
            return httpx.Response(200, json={"task_id": "task-1"})
        if request.url.path == "/api/content-index/video/quote":
            return httpx.Response(200, json=_quote())
        if request.url.path == "/api/content-index/video/tasks/task-1":
            return httpx.Response(
                200,
                json={
                    "task_id": "task-1",
                    "status": "SUCCEEDED",
                    "video_url": "https://result.example/scene.mp4",
                    "error": "",
                },
            )
        if request.url.path == "/api/content-index/video/tts":
            return httpx.Response(
                200,
                json={"audio_url": "https://result.example/voice.mp3"},
            )
        raise AssertionError(request.url.path)

    client = AdsMediaGatewayClient(
        "https://ads.example",
        "uploader",
        "secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        quote = client.quote("preview")
        assert client.submit_scene(
            "全新生成镜头",
            5,
            "preview",
            quote["quote_id"],
        )["task_id"] == "task-1"
        assert client.get_task("task-1")["status"] == "SUCCEEDED"
        assert client.synthesize_voice("重写旁白")["audio_url"].endswith(".mp3")
    finally:
        client.close()
    encoded = b"".join(request.content for request in requests).decode("utf-8")
    assert "/Users/" not in encoded and "原口播全文" not in encoded


def test_renderer_burns_subtitles_without_reading_source_video(tmp_path, monkeypatch):
    """本地合成只读取 AI 片段与配音，不读取参考原片。"""
    scene_paths = []
    for index in range(4):
        path = tmp_path / f"scene-{index}.mp4"
        path.write_bytes(b"generated")
        scene_paths.append(path)
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"voice")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"final")

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr("app.content_video_pipeline.subprocess.run", run)
    renderer = LocalVideoRenderer(ffmpeg_path="/tools/ffmpeg")
    result = renderer.render(
        source_id="asset-1",
        plan=_plan(),
        scene_paths=scene_paths,
        voice_path=voice,
        output_root=tmp_path / "output",
    )
    assert result.is_file()
    joined = " ".join(str(part) for command in commands for part in command)
    assert "subtitles=" in joined
    assert "source.mp4" not in joined


def test_video_job_is_persisted_and_visible_in_asset_list(tmp_path):
    """重启应用后仍能看到分镜、费用和进度。"""
    store = ContentIndexStore(tmp_path / "content.sqlite")
    store.upsert_asset(
        {
            "source_id": "asset-1",
            "author": "a",
            "source_title": "source",
            "source_path": "/local/source.mp4",
            "transcript_path": "/local/transcript.txt",
            "sha256": "x",
            "size_bytes": 1,
            "mtime": 1,
        }
    )
    store.save_video_plan(
        "asset-1",
        _plan(),
        "deepseek",
        estimated_cost_cny=4.8,
    )
    row = store.list_assets()[0]
    assert row["video_status"] == "planned"
    assert row["video_estimated_cost_cny"] == 4.8
    assert row["video_plan"]["rights_mode"] == "regenerate"
    store.update_video_job(
        "asset-1",
        status="ready",
        progress=100,
        output_path="/local/output/final.mp4",
    )
    assert store.get_video_job("asset-1")["progress"] == 100


def test_video_job_claim_is_atomic_and_active_plan_is_immutable(tmp_path):
    """并发只能领取一次，排队中的分镜不能被 AI 重写覆盖。"""
    database = tmp_path / "content.sqlite"
    store = ContentIndexStore(database)
    store.save_video_plan("asset-1", _plan(), "deepseek", estimated_cost_cny=4.8)
    assert store.claim_video_job("asset-1", "preview", _quote()) is True
    assert ContentIndexStore(database).claim_video_job(
        "asset-1",
        "preview",
        _quote(),
    ) is False
    changed = _plan()
    changed["title"] = "不应覆盖"
    store.save_video_plan("asset-1", changed, "deepseek", estimated_cost_cny=4.8)
    assert store.get_video_job("asset-1")["plan"]["title"] != "不应覆盖"


def test_generate_api_requires_explicit_cost_confirmation(tmp_path, monkeypatch):
    """任何付费视频调用前必须由用户确认估算费用。"""
    database = tmp_path / "content.sqlite"
    store = ContentIndexStore(database)
    store.upsert_asset(
        {
            "source_id": "asset-1",
            "author": "a",
            "source_title": "source",
            "source_path": "/local/source.mp4",
            "transcript_path": "/local/transcript.txt",
            "sha256": "x",
            "size_bytes": 1,
            "mtime": 1,
        }
    )
    store.save_video_plan(
        "asset-1",
        _plan(),
        "deepseek",
        estimated_cost_cny=4.8,
    )
    monkeypatch.setattr(
        content_api,
        "content_paths",
        lambda: (tmp_path / "share", tmp_path / "inbox", database),
    )

    async def scenario():
        transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
        ) as client:
            response = await client.post(
                "/api/content-index/video/asset-1/generate",
                json={"quality": "preview", "confirm_cost": False},
            )
            assert response.status_code == 400
            assert "确认" in response.json()["detail"]

    asyncio.run(scenario())


def test_full_mock_video_job_reaches_ready_without_source_upload(tmp_path, monkeypatch):
    """模拟 Wan→配音→本地合成整链路，不产生模型费用。"""
    database = tmp_path / "content.sqlite"
    store = ContentIndexStore(database)
    store.upsert_asset(
        {
            "source_id": "asset-1",
            "author": "a",
            "source_title": "source",
            "source_path": "/private/reference-source.mp4",
            "transcript_path": "/private/reference-transcript.txt",
            "sha256": "x",
            "size_bytes": 1,
            "mtime": 1,
        }
    )
    store.save_video_plan(
        "asset-1",
        _plan(),
        "deepseek",
        estimated_cost_cny=4.8,
    )
    store.claim_video_job("asset-1", "preview", _quote())
    monkeypatch.setattr(
        content_api,
        "content_paths",
        lambda: (tmp_path / "share", tmp_path / "inbox", database),
    )
    monkeypatch.setattr(content_api, "VIDEO_OUTPUT_ROOT", tmp_path / "output")

    class FakeGateway:
        prompts = []

        def submit_scene(self, prompt, duration, quality, quote_id):
            self.prompts.append(prompt)
            return {"task_id": f"task-{len(self.prompts)}"}

        def get_task(self, task_id):
            return {
                "status": "SUCCEEDED",
                "video_url": f"https://generated.example/{task_id}.mp4",
            }

        def synthesize_voice(self, text):
            return {"audio_url": "https://generated.example/voice.mp3"}

        def close(self):
            return None

    gateway = FakeGateway()
    monkeypatch.setattr(content_api, "_ads_media_client", lambda: gateway)

    def download(url, path, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"generated")

    monkeypatch.setattr(content_api, "_download_generated_file", download)
    monkeypatch.setattr(content_api, "_find_ffmpeg", lambda: "/tools/ffmpeg")

    def render(self, **kwargs):
        final = kwargs["output_root"] / kwargs["source_id"] / "final.mp4"
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"final")
        return final

    monkeypatch.setattr(LocalVideoRenderer, "render", render)
    content_api._run_video_job("asset-1", "preview")
    job = ContentIndexStore(database).get_video_job("asset-1")
    assert job["status"] == "ready" and job["progress"] == 100
    assert len(gateway.prompts) == 4
    sent = " ".join(gateway.prompts)
    assert "reference-source.mp4" not in sent
    assert "reference-transcript.txt" not in sent


def test_retry_reuses_paid_scene_and_downloaded_files(tmp_path, monkeypatch):
    """失败重试不得重复购买已提交或已下载的 Wan 场景。"""
    database = tmp_path / "content.sqlite"
    store = ContentIndexStore(database)
    store.upsert_asset(
        {
            "source_id": "asset-1",
            "author": "a",
            "source_title": "source",
            "source_path": "/private/source.mp4",
            "transcript_path": "/private/transcript.txt",
            "sha256": "x",
            "size_bytes": 1,
            "mtime": 1,
        }
    )
    store.save_video_plan("asset-1", _plan(), "deepseek", estimated_cost_cny=4.8)
    store.claim_video_job("asset-1", "preview", _quote())
    output = tmp_path / "output"
    workspace = (
        output
        / "asset-1"
        / "work"
        / "preview"
        / content_api._plan_hash(_plan())
    )
    workspace.mkdir(parents=True)
    (workspace / "scene-1.mp4").write_bytes(b"existing")
    (workspace / "scene-2.task.json").write_text(
        '{"task_id": "already-paid-task"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        content_api,
        "content_paths",
        lambda: (tmp_path / "share", tmp_path / "inbox", database),
    )
    monkeypatch.setattr(content_api, "VIDEO_OUTPUT_ROOT", output)

    class FakeGateway:
        submitted = []
        queried = []

        def submit_scene(self, prompt, duration, quality, quote_id):
            self.submitted.append(prompt)
            return {"task_id": f"new-{len(self.submitted)}"}

        def get_task(self, task_id):
            self.queried.append(task_id)
            return {
                "status": "SUCCEEDED",
                "video_url": f"https://generated.example/{task_id}.mp4",
            }

        def synthesize_voice(self, text):
            return {"audio_url": "https://generated.example/voice.mp3"}

        def close(self):
            return None

    gateway = FakeGateway()
    monkeypatch.setattr(content_api, "_ads_media_client", lambda: gateway)

    def download(url, path, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"generated")

    monkeypatch.setattr(content_api, "_download_generated_file", download)
    monkeypatch.setattr(content_api, "_find_ffmpeg", lambda: "/tools/ffmpeg")

    def render(self, **kwargs):
        final = kwargs["output_root"] / kwargs["source_id"] / "final.mp4"
        final.write_bytes(b"final")
        return final

    monkeypatch.setattr(LocalVideoRenderer, "render", render)
    content_api._run_video_job("asset-1", "preview")
    assert gateway.queried[0] == "already-paid-task"
    assert len(gateway.submitted) == 2


def test_uncertain_submission_stops_without_second_paid_call(tmp_path, monkeypatch):
    """上次提交结果不确定时必须停住，不能自动再次扣费。"""
    database = tmp_path / "content.sqlite"
    store = ContentIndexStore(database)
    store.save_video_plan("asset-1", _plan(), "deepseek", estimated_cost_cny=4.8)
    store.claim_video_job("asset-1", "preview", _quote())
    output = tmp_path / "output"
    workspace = (
        output
        / "asset-1"
        / "work"
        / "preview"
        / content_api._plan_hash(_plan())
    )
    workspace.mkdir(parents=True)
    (workspace / "scene-1.task.json").write_text(
        '{"state":"submitting","quote_id":"' + ("a" * 64) + '"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        content_api,
        "content_paths",
        lambda: (tmp_path / "share", tmp_path / "inbox", database),
    )
    monkeypatch.setattr(content_api, "VIDEO_OUTPUT_ROOT", output)

    class FakeGateway:
        submitted = 0

        def submit_scene(self, *args):
            self.submitted += 1
            raise AssertionError("must not resubmit")

        def close(self):
            return None

    gateway = FakeGateway()
    monkeypatch.setattr(content_api, "_ads_media_client", lambda: gateway)
    content_api._run_video_job("asset-1", "preview")
    job = ContentIndexStore(database).get_video_job("asset-1")
    assert job["status"] == "failed"
    assert "避免重复扣费" in job["error"]
    assert gateway.submitted == 0


def test_generated_download_blocks_ssrf_and_upgrades_aliyun_http(tmp_path, monkeypatch):
    """模型结果下载只允许阿里云结果域名，并把文档中的 HTTP 地址升级为 HTTPS。"""
    with pytest.raises(PipelineError, match="地址无效"):
        content_api._download_generated_file(
            "http://127.0.0.1/private",
            tmp_path / "blocked.mp4",
            max_bytes=100,
        )
    captured = []

    class FakeResponse:
        status_code = 200
        headers = {}
        url = httpx.URL(
            "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/voice.mp3"
        )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield b"ID3generated"

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def stream(self, method, url):
            captured.append(url)
            return FakeResponse()

        def close(self):
            return None

    monkeypatch.setattr(content_api.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        content_api.socket,
        "getaddrinfo",
        lambda *args: [(2, 1, 6, "", ("8.8.8.8", 443))],
    )
    target = tmp_path / "voice.mp3"
    content_api._download_generated_file(
        "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/voice.mp3",
        target,
        max_bytes=100,
    )
    assert captured == [
        "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/voice.mp3"
    ]
    assert target.read_bytes() == b"ID3generated"

    class OversizeResponse(FakeResponse):
        def iter_bytes(self):
            yield b"ID3" + b"x" * 200

    class OversizeClient(FakeClient):
        def stream(self, method, url):
            return OversizeResponse()

    monkeypatch.setattr(content_api.httpx, "Client", OversizeClient)
    failed = tmp_path / "failed.mp3"
    with pytest.raises(PipelineError, match="安全上限"):
        content_api._download_generated_file(
            "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/voice.mp3",
            failed,
            max_bytes=20,
        )
    assert not failed.exists()
    assert not (tmp_path / "failed.mp3.part").exists()
