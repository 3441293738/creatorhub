"""本机 AI 二创视频计划、网关与渲染回归。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
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
    steps = [
        (
            "查看缺缸异响怠速",
            "发动机怠速剧烈抖动",
            "hand_insert 指向抖动区域",
            "open",
            "杰克逊滑步致敬冷幽默入画，棒球帽檐 MAXCOOL 一闪，胸标 Kaka Auto Parts 可读",
        ),
        (
            "对比已换火花塞仍抖动",
            "火花塞与喷油嘴区域仍异常",
            "cutaway_host 短切讲解",
            "mid",
            "操作中袖口 MAXCOOL 自然入画",
        ),
        (
            "指向关键传感器插头",
            "曲轴位置传感器插头与线束",
            "over_shoulder 看向插头",
            "mid",
            "肩后可见工服标识不抢戏",
        ),
        (
            "拔插头验证怠速变稳",
            "拔掉传感器插头后怠速变稳",
            "hand_insert 完成拔插",
            "close",
            "碟中谍滑索致敬冷幽默出画，痞帅痞笑被向上一点点拉出画外，胸前品牌 lockup",
        ),
    ]
    scenes = []
    for index, (process, structure, insert, brand_beat, brand_note) in enumerate(
        steps, start=1
    ):
        scenes.append(
            {
                "duration": 5,
                "beat": f"第{index}段教学接力",
                "brand_beat": brand_beat,
                "transcript_anchor": f"口播第{index}节：{process}",
                "core_claim": f"核心判断{index}：{process}",
                "process_step": process,
                "structure_focus": structure,
                "detail_checklist": [
                    f"细节{index}a：关键面清晰",
                    f"细节{index}b：动作前后状态可对比",
                ],
                "character_insert": insert,
                "camera_move": f"第{index}段缓推连续运镜落到操作点",
                "visual_prompt": (
                    f"连续竖屏镜头 {index}：发动机舱结构主体，{structure}，"
                    f"执行{process}，{brand_note}，浅景深但操作面清晰，无商标无水印"
                ),
                "beat_ids": [f"B{index:02d}"],
                "narration": f"这是第{index}段对齐口播的旁白。",
                "narration_en": f"Beat {index}: verify the sensor connector, then watch idle.",
            }
        )
    source_beats = [
        {
            "beat_id": f"B{index:02d}",
            "source_quote": f"原稿第{index}条：{process}",
            "corrected_fact": process,
            "evidence_status": "source_supported",
        }
        for index, (process, *_rest) in enumerate(steps, start=1)
    ]
    coverage_check = [
        {
            "beat_id": beat["beat_id"],
            "copy_cn_sentence": f"第{index}项。",
            "copy_en_sentence": f"Beat {index}.",
            "covered": True,
            "no_new_fact": True,
            "no_causal_change": True,
        }
        for beat in source_beats
    ]
    return {
        "title": "发动机缺缸异响一招排查",
        "title_en": "Rough idle? Check this sensor before a rebuild",
        "hook": "怠速抖得像拖拉机，先别急着大修。",
        "hook_en": "Idle shakes hard — don't rebuild yet. Watch this connector.",
        "rights_mode": "regenerate",
        "direction": "一镜到底商业广告科普：品牌前中后；入出场冷幽默大片致敬",
        "core_arc": "异响现象→乱修无效→定位传感器→拔插头验证→结论",
        "character_lock": (
            "以品牌本人真实样貌为底，做年轻化与轻微美化：脸型更利落、皮肤更干净；"
            "后脑勺轮廓略更饱满圆润；仍可辨认为本人，半框眼镜可保留。"
            "神态冷静专注，眼神盯零件不游离；出场可痞帅痞笑；"
            "穿着带 Kaka Auto Parts 与 MAXCOOL 标识的专业汽修工服。"
        ),
        "look_variant": "no_hat",
        "look": "一镜到底感商业广告汽修科普，侧逆光；入出场特色 setpiece，竖屏 9:16",
        "branding": "Kaka Auto Parts + MAXCOOL；前开场露出、中段操作穿插、末段 lockup + 痞帅痞笑",
        "setpiece_open": "致敬杰克逊滑步冷幽默入场：棒球帽檐 MAXCOOL 一闪再蹲进机舱",
        "setpiece_close": "致敬碟中谍滑索冷幽默出场：痞帅痞笑被向上一点点拉出画外 + lockup",
        "source_beats": source_beats,
        "copy_cn": " ".join(item["copy_cn_sentence"] for item in coverage_check),
        "copy_en": " ".join(item["copy_en_sentence"] for item in coverage_check),
        "coverage_check": coverage_check,
        "episode_count": 1,
        "active_episode": 1,
        "episode_plan": [{"episode": 1, "beat_ids": [item["beat_id"] for item in source_beats]}],
        "scenes": scenes,
    }


def _quote() -> dict:
    """返回一份服务端预览报价。"""
    return {
        "quote_id": "a" * 64,
        "quality": "preview",
        "model": "wanx2.1-t2v-turbo",
        "scene_count": 4,
        "scene_duration_seconds": 5,
        "scene_durations": [5, 5, 5, 5],
        "total_cost_cny": 3.0,
        "generation_id": "b" * 32,
    }


class FakeJSONCaller:
    """返回固定视频计划的文字模型替身。"""

    def call_json(self, prompt: str) -> tuple[dict, str]:
        """返回测试计划。"""
        assert "禁止复用原视频画面" in prompt
        assert "品牌本人" in prompt
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
    assert estimate_plan_cost(plan, "preview") == 0.82


def test_video_plan_rejects_source_footage_reuse():
    """任何复用、裁剪或上传原片的计划都必须被门禁拒绝。"""
    unsafe = _plan()
    unsafe["rights_mode"] = "reuse_source"
    with pytest.raises(PipelineError, match="重新生成"):
        VideoPlanService.guard_plan(unsafe)


def test_video_plan_requires_locked_brand_self_likeness():
    """多条成片必须锁定品牌本人年轻化形象，禁止漂移成路人。"""
    plan = _plan()
    guarded = VideoPlanService.guard_plan(plan)
    assert "品牌本人" in guarded["character_lock"]
    assert guarded["look_variant"] in {"no_hat", "with_hat"}
    assert all(scene["structure_focus"] for scene in guarded["scenes"])
    broken = _plan()
    broken["character_lock"] = "普通路人修车师傅"
    with pytest.raises(PipelineError, match="品牌本人"):
        VideoPlanService.guard_plan(broken)


def test_video_plan_requires_verifiable_process_details():
    """流程与细节必须能慢放核对，不能只是好看的修车氛围。"""
    plan = _plan()
    guarded = VideoPlanService.guard_plan(plan)
    assert "一镜到底" in guarded["direction"]
    assert guarded["core_arc"]
    for scene in guarded["scenes"]:
        assert scene["process_step"]
        assert scene["transcript_anchor"]
        assert scene["core_claim"]
        assert len(scene["detail_checklist"]) >= 2
        assert scene["camera_move"]
    weak = _plan()
    weak["scenes"][0]["detail_checklist"] = ["只有一条"]
    with pytest.raises(PipelineError, match="detail_checklist"):
        VideoPlanService.guard_plan(weak)
    vague = _plan()
    vague["scenes"][1]["process_step"] = "展示氛围感车间"
    with pytest.raises(PipelineError, match="可执行动作"):
        VideoPlanService.guard_plan(vague)


def test_build_render_prompt_locks_transcript_process_and_details():
    """提交生成时必须把口播锚点与可核对步骤打进提示词。"""
    plan = _plan()
    scene = plan["scenes"][3]
    prompt = VideoPlanService.build_render_prompt(plan, scene)
    assert "TRANSCRIPT ANCHOR" in prompt
    assert scene["process_step"] in prompt
    assert scene["structure_focus"] in prompt
    assert "DETAILS THAT MUST SURVIVE" in prompt
    assert "Do NOT invent a different fault" in prompt
    assert "never vacant/wandering" in prompt
    assert "No long talking-head" in prompt or "lip-sync" in prompt
    assert "BRAND CLOSE" in prompt
    assert "SETPIECE" in prompt
    assert "rogue smirk" in prompt or "痞帅痞笑" in prompt
    assert "composited later" in prompt
    assert "NO on-screen text" in prompt
    assert "Kaka Auto Parts" not in prompt and "MAXCOOL" not in prompt
    assert "碟中谍" in prompt or "滑索" in prompt


def test_video_plan_requires_brand_beats_open_mid_close():
    """品牌植入必须前中后都有，入出场要有冷幽默大片致敬。"""
    plan = _plan()
    guarded = VideoPlanService.guard_plan(plan)
    beats = {scene["brand_beat"] for scene in guarded["scenes"]}
    assert beats == {"open", "mid", "close"}
    assert guarded["setpiece_open"]
    assert guarded["setpiece_close"]
    missing_close = _plan()
    missing_close["scenes"][3]["brand_beat"] = "mid"
    missing_close["scenes"][3]["visual_prompt"] = (
        "连续竖屏：拔插头验证怠速，操作面清晰，无品牌落点"
    )
    with pytest.raises(PipelineError, match="前中后"):
        VideoPlanService.guard_plan(missing_close)
    weak_close = _plan()
    weak_close["scenes"][3]["visual_prompt"] = (
        "连续竖屏：拔插头验证怠速，胸前 Kaka Auto Parts 标识可读，挥手硬广推销结束"
    )
    with pytest.raises(PipelineError, match="冷幽默|大片致敬"):
        VideoPlanService.guard_plan(weak_close)
    same_setpiece = _plan()
    same_setpiece["setpiece_close"] = same_setpiece["setpiece_open"]
    with pytest.raises(PipelineError, match="不能雷同"):
        VideoPlanService.guard_plan(same_setpiece)


def test_video_plan_limits_face_on_host_cuts():
    """正脸讲解整片最多一段，其余用手部/肩后穿插降露怯。"""
    plan = _plan()
    plan["scenes"][0]["character_insert"] = "cutaway_host 短切"
    plan["scenes"][2]["character_insert"] = "cutaway_host 再讲一次"
    with pytest.raises(PipelineError, match="cutaway_host"):
        VideoPlanService.guard_plan(plan)
    ok = _plan()
    ok["scenes"][1]["character_insert"] = "cutaway_host 仅一次"
    guarded = VideoPlanService.guard_plan(ok)
    assert sum("cutaway_host" in s["character_insert"] for s in guarded["scenes"]) == 1
    no_eyes = _plan()
    no_eyes["character_lock"] = (
        "以品牌本人真实样貌为底，做年轻化美化，穿着 Kaka Auto Parts 与 MAXCOOL 工服。"
    )
    with pytest.raises(PipelineError, match="专注眼神"):
        VideoPlanService.guard_plan(no_eyes)


def test_video_plan_requires_localized_cn_en_copy():
    """国内中文与海外英文文案都要有，且拒绝英文吆喝腔。"""
    plan = _plan()
    guarded = VideoPlanService.guard_plan(plan)
    assert guarded["title_en"]
    assert guarded["hook_en"]
    assert guarded["copy_cn"] and guarded["copy_en"]
    assert all(scene["narration_en"] for scene in guarded["scenes"])
    missing = _plan()
    missing["hook_en"] = ""
    with pytest.raises(PipelineError, match="英文标题或钩子"):
        VideoPlanService.guard_plan(missing)
    hype = _plan()
    hype["scenes"][0]["narration_en"] = "Must see this 100% fix amazing!!!"
    with pytest.raises(PipelineError, match="英文旁白"):
        VideoPlanService.guard_plan(hype)


def test_video_plan_requires_complete_source_beat_coverage():
    """每条原稿事实必须映射到中英文文案，且不得新增事实或改变因果。"""
    guarded = VideoPlanService.guard_plan(_plan())
    assert [item["beat_id"] for item in guarded["source_beats"]] == [
        "B01",
        "B02",
        "B03",
        "B04",
    ]
    assert all(item["covered"] for item in guarded["coverage_check"])

    missing = _plan()
    missing["coverage_check"].pop()
    with pytest.raises(PipelineError, match="覆盖"):
        VideoPlanService.guard_plan(missing)

    invented = _plan()
    invented["coverage_check"][0]["no_new_fact"] = False
    with pytest.raises(PipelineError, match="新增事实"):
        VideoPlanService.guard_plan(invented)


def test_video_plan_splits_more_than_four_beats_into_episode_map():
    """信息点超过四条必须分集，不能为凑 20 秒删除原稿事实。"""
    plan = _plan()
    extra_beats = []
    for index in range(5, 9):
        beat = {
            "beat_id": f"B{index:02d}",
            "source_quote": f"原稿第{index}个易坏件及故障现象",
            "corrected_fact": f"第{index}个易坏件及故障现象",
            "evidence_status": "source_supported",
        }
        extra_beats.append(beat)
        plan["coverage_check"].append(
            {
                "beat_id": beat["beat_id"],
                "copy_cn_sentence": f"中文覆盖{beat['corrected_fact']}。",
                "copy_en_sentence": f"English covers fact {index}.",
                "covered": True,
                "no_new_fact": True,
                "no_causal_change": True,
            }
        )
    plan["source_beats"].extend(extra_beats)
    plan["copy_cn"] += " " + " ".join(
        item["copy_cn_sentence"] for item in plan["coverage_check"][4:]
    )
    plan["copy_en"] += " " + " ".join(
        item["copy_en_sentence"] for item in plan["coverage_check"][4:]
    )
    plan["episode_count"] = 2
    plan["episode_plan"] = [
        {"episode": 1, "beat_ids": ["B01", "B02", "B03", "B04"]},
        {"episode": 2, "beat_ids": ["B05", "B06", "B07", "B08"]},
    ]
    guarded = VideoPlanService.guard_plan(plan)
    assert guarded["episode_count"] == 2
    assert guarded["episode_plan"][1]["beat_ids"] == ["B05", "B06", "B07", "B08"]
    assert len(guarded["source_beats"]) == 8


def test_episode_plan_uses_realistic_spoken_duration_budget():
    """长信息点即使不满四条也必须分集，单集口播估算不得超过30秒。"""
    coverage = [
        {
            "beat_id": "B01",
            "copy_cn_sentence": "节温器安装在发动机缸体上，水温高、暖风不热、高速油耗偏高时，需要检查冷却系统和节温器。",
        },
        {
            "beat_id": "B02",
            "copy_cn_sentence": "喷油嘴安装在进气歧管上，发动机抖动、油耗偏高、启动困难时，需要检查喷油嘴和供油状态。",
        },
        {
            "beat_id": "B03",
            "copy_cn_sentence": "刹车真空单向阀位于真空管路中间，刹车踏板发硬或高速制动效果异常时，要检查真空管路和单向阀。",
        },
    ]
    episodes = VideoPlanService.build_episode_plan(coverage)
    assert [item["beat_ids"] for item in episodes] == [
        ["B01", "B02"],
        ["B03"],
    ]
    assert all(item["estimated_seconds"] <= 30 for item in episodes)


def test_scene_durations_cover_active_episode_spoken_budget():
    """四段镜头可变时长，但必须覆盖当前分集口播估时且单段不少于4秒。"""
    plan = _plan()
    plan["scenes"][0]["duration"] = 8
    guarded = VideoPlanService.guard_plan(plan)
    assert guarded["duration_seconds"] == 23

    too_short = _plan()
    too_short["scenes"][0]["duration"] = 3
    with pytest.raises(PipelineError, match="4～15"):
        VideoPlanService.guard_plan(too_short)


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
                "generation_id",
                "scene_index",
                "look_variant",
            }
            assert body["cost_confirmed"] is True
            assert body["look_variant"] in {"no_hat", "with_hat"}
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
            "b" * 32,
            1,
        )["task_id"] == "task-1"
        assert client.get_task("task-1")["status"] == "SUCCEEDED"
        assert client.synthesize_voice("重写旁白")["audio_url"].endswith(".mp3")
    finally:
        client.close()
    encoded = b"".join(request.content for request in requests).decode("utf-8")
    assert "/Users/" not in encoded and "原口播全文" not in encoded


def test_renderer_does_not_generate_or_burn_subtitles(tmp_path, monkeypatch):
    """本地合成只混入配音，禁止生成 SRT 或向画面烧录字幕。"""
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
    assert "subtitles=" not in joined
    assert not (tmp_path / "output" / "asset-1" / "subtitles.srt").exists()
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
        estimated_cost_cny=3.0,
    )
    row = store.list_assets()[0]
    assert row["video_status"] == "planned"
    assert row["video_estimated_cost_cny"] == 3.0
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
    store.save_video_plan("asset-1", _plan(), "deepseek", estimated_cost_cny=3.0)
    assert store.claim_video_job("asset-1", "preview", _quote()) is True
    assert store.get_video_job("asset-1")["generation_id"] == _quote()["generation_id"]
    assert ContentIndexStore(database).claim_video_job(
        "asset-1",
        "preview",
        _quote(),
    ) is False
    changed = _plan()
    changed["title"] = "不应覆盖"
    store.save_video_plan("asset-1", changed, "deepseek", estimated_cost_cny=3.0)
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
        estimated_cost_cny=3.0,
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


def test_generate_video_revalidates_exact_generation_quote(monkeypatch):
    """制作时必须用用户确认过的同一批次重新验价。"""
    quote = _quote()

    class FakeStore:
        def get_video_job(self, source_id):
            return {"status": "planned"}

        def claim_video_job(self, source_id, quality, current_quote):
            return current_quote == quote

    class FakeGateway:
        def __init__(self):
            self.generation_id = ""

        def quote(self, quality, generation_id="", scene_durations=None):
            self.generation_id = generation_id
            return quote

        def close(self):
            return None

    gateway = FakeGateway()
    monkeypatch.setattr(content_api, "content_store", lambda: FakeStore())
    monkeypatch.setattr(content_api, "_ads_media_client", lambda: gateway)
    monkeypatch.setattr(content_api, "_run_video_job", lambda *args: None)
    response = asyncio.run(
        content_api.generate_content_video(
            "asset-1",
            {
                "quality": "preview",
                "confirm_cost": True,
                "quote_id": quote["quote_id"],
                "generation_id": quote["generation_id"],
            },
        )
    )
    assert response.status_code == 202
    assert gateway.generation_id == quote["generation_id"]


def test_generate_video_rejects_changed_generation_before_claim(monkeypatch):
    """服务端返回不同批次时，必须在启动付费任务前阻断。"""
    quote = _quote()
    changed = {**quote, "generation_id": "c" * 32}

    class FakeStore:
        def get_video_job(self, source_id):
            return {"status": "planned"}

        def claim_video_job(self, *args):
            raise AssertionError("批次不一致时不得认领付费任务")

    class FakeGateway:
        def quote(self, quality, generation_id="", scene_durations=None):
            return changed

        def close(self):
            return None

    monkeypatch.setattr(content_api, "content_store", lambda: FakeStore())
    monkeypatch.setattr(content_api, "_ads_media_client", FakeGateway)
    response = asyncio.run(
        content_api.generate_content_video(
            "asset-1",
            {
                "quality": "preview",
                "confirm_cost": True,
                "quote_id": quote["quote_id"],
                "generation_id": quote["generation_id"],
            },
        )
    )
    assert response.status_code == 409


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
        estimated_cost_cny=3.0,
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

        def submit_scene(
            self,
            prompt,
            duration,
            quality,
            quote_id,
            generation_id,
            scene_index,
            look_variant="no_hat",
            scene_durations=None,
        ):
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
    store.save_video_plan("asset-1", _plan(), "deepseek", estimated_cost_cny=3.0)
    store.claim_video_job("asset-1", "preview", _quote())
    generation_id = store.get_video_job("asset-1")["generation_id"]
    output = tmp_path / "output"
    workspace = (
        output
        / "asset-1"
        / "work"
        / "preview"
        / content_api._plan_hash(_plan())
        / generation_id
    )
    workspace.mkdir(parents=True)
    (workspace / "scene-1.mp4").write_bytes(b"existing")
    (workspace / "scene-2.task.json").write_text(
        json.dumps(
            {
                "state": "submitted",
                "task_id": "already-paid-task",
                "generation_id": generation_id,
            }
        ),
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

        def submit_scene(
            self,
            prompt,
            duration,
            quality,
            quote_id,
            generation_id,
            scene_index,
            look_variant="no_hat",
            scene_durations=None,
        ):
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


def test_paid_polling_timeout_stops_without_new_generation(tmp_path, monkeypatch):
    """已付费场景轮询超时必须锁定，不能换批次再次扣费。"""
    database = tmp_path / "content.sqlite"
    store = ContentIndexStore(database)
    store.save_video_plan("asset-1", _plan(), "deepseek", estimated_cost_cny=3.0)
    store.claim_video_job("asset-1", "preview", _quote())
    generation_id = store.get_video_job("asset-1")["generation_id"]
    output = tmp_path / "output"
    workspace = (
        output
        / "asset-1"
        / "work"
        / "preview"
        / content_api._plan_hash(_plan())
        / generation_id
    )
    workspace.mkdir(parents=True)
    (workspace / "scene-1.task.json").write_text(
        json.dumps(
            {
                "state": "submitting",
                "quote_id": "a" * 64,
                "generation_id": generation_id,
                "scene_index": 1,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        content_api,
        "content_paths",
        lambda: (tmp_path / "share", tmp_path / "inbox", database),
    )
    monkeypatch.setattr(content_api, "VIDEO_OUTPUT_ROOT", output)

    class FakeGateway:
        replays = 0

        def submit_scene(self, *args, **kwargs):
            self.replays += 1
            return {"task_id": "existing-paid-task"}

        def close(self):
            return None

    gateway = FakeGateway()
    monkeypatch.setattr(content_api, "_ads_media_client", lambda: gateway)
    monkeypatch.setattr(
        content_api,
        "_wait_video_scene",
        lambda *args: (_ for _ in ()).throw(
            PipelineError("视频模型等待超过十分钟")
        ),
    )
    content_api._run_video_job("asset-1", "preview")
    job = ContentIndexStore(database).get_video_job("asset-1")
    assert job["status"] == "uncertain"
    assert "等待超过十分钟" in job["error"]
    assert gateway.replays == 1
    assert ContentIndexStore(database).claim_video_job(
        "asset-1",
        "preview",
        _quote(),
    ) is False


def test_startup_recovery_retries_database_and_resumes_job(monkeypatch):
    """应用启动恢复不依赖打开页面，数据库瞬时失败会重试。"""
    attempts = []
    resumed = []

    class FakeStore:
        def list_incomplete_video_jobs(self):
            attempts.append(True)
            if len(attempts) < 3:
                raise sqlite3.OperationalError("busy")
            return [{"source_id": "asset-1", "quality": "preview"}]

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(content_api, "content_store", lambda: FakeStore())
    monkeypatch.setattr(content_api.asyncio, "sleep", no_wait)
    monkeypatch.setattr(
        content_api,
        "_run_video_job",
        lambda source_id, quality: resumed.append((source_id, quality)),
    )

    async def scenario():
        count = await content_api.resume_incomplete_video_jobs()
        tasks = list(content_api._VIDEO_BACKGROUND_TASKS)
        if tasks:
            await asyncio.gather(*tasks)
        return count

    assert asyncio.run(scenario()) == 1
    assert len(attempts) == 3
    assert resumed == [("asset-1", "preview")]


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


def test_generated_download_accepts_seedance_volces_result(tmp_path, monkeypatch):
    """Seedance 结果域名通过同一套公网与文件头门禁。"""
    class FakeResponse:
        status_code = 200
        headers = {}
        url = httpx.URL("https://seedance-output.tos-cn-beijing.volces.com/video.mp4")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield b"\x00\x00\x00\x18ftypmp42generated"

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def stream(self, method, url):
            return FakeResponse()

        def close(self):
            return None

    monkeypatch.setattr(content_api.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        content_api.socket,
        "getaddrinfo",
        lambda *args: [(2, 1, 6, "", ("8.8.8.8", 443))],
    )
    target = tmp_path / "seedance.mp4"
    content_api._download_generated_file(
        "https://seedance-output.tos-cn-beijing.volces.com/video.mp4",
        target,
        max_bytes=100,
    )
    assert target.is_file()
