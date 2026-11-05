"""参考内容转全新 AI 视频的本机计划、网关和渲染组件。"""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

from .content_pipeline import JSONCaller, PROMPT_ROOT, PipelineError


SCENE_COUNT = 4
MIN_SCENE_DURATION_SECONDS = 4
MAX_SCENE_DURATION_SECONDS = 15
LEGACY_GATEWAY_SCENE_DURATION_SECONDS = 5
QUALITY_COST_CNY_PER_SECOND = {
    "preview": 0.041,  # Seedance 1.0 pro-fast 9:16 480p 估算
    "quality": 0.0864,  # Seedance 1.0 pro-fast 9:16 720p 估算
}


def estimate_plan_cost(plan: dict, quality: str) -> float:
    """按模型公开单价估算视频费用，不包含极低的文字与配音费用。"""
    if quality not in QUALITY_COST_CNY_PER_SECOND:
        raise PipelineError("未知视频质量档位")
    duration = sum(int(scene.get("duration") or 0) for scene in plan.get("scenes") or [])
    return round(duration * QUALITY_COST_CNY_PER_SECOND[quality], 2)


class VideoPlanService:
    """用文字模型把参考口播改写成安全的全新视频分镜。"""

    NARRATION_CHARS_PER_SECOND = 4.2
    EPISODE_OVERHEAD_SECONDS = 4
    MAX_EPISODE_SECONDS = 30

    def __init__(self, caller: JSONCaller):
        self.caller = caller

    def create(self, metadata: dict, transcript: str) -> Tuple[dict, str]:
        """生成并校验四段竖屏视频计划。"""
        try:
            template = (PROMPT_ROOT / "video_plan.txt").read_text(encoding="utf-8")
        except OSError as exc:
            raise PipelineError("缺少视频分镜提示词") from exc
        prompt = (
            template.replace(
                "{{metadata}}",
                json.dumps(metadata, ensure_ascii=False),
            )
            .replace("{{transcript}}", transcript)
        )
        raw, provider = self.caller.call_json(prompt)
        return self.guard_plan(raw), provider

    _INSERT_MODES = {
        "cutaway_host",
        "over_shoulder",
        "hand_insert",
        "silhouette_pass",
    }

    _STRUCTURE_TOKENS = (
        "engine",
        "sensor",
        "connector",
        "intake",
        "brake",
        "spark",
        "manifold",
        "wiring",
        "plug",
        "idle",
        "结构",
        "插头",
        "传感",
        "发动机",
        "刹车",
        "火花塞",
        "喷油",
        "怠速",
        "线束",
        "卡扣",
    )

    @staticmethod
    def _normalize_checklist(raw_value) -> List[str]:
        """把细节核对清单收成 2～4 条短句。"""
        items: List[str] = []
        if isinstance(raw_value, list):
            items = [str(item).strip() for item in raw_value]
        elif isinstance(raw_value, str):
            text = raw_value.strip()
            if text:
                for sep in ("；", ";", "\n", "、"):
                    if sep in text:
                        items = [part.strip() for part in text.split(sep)]
                        break
                else:
                    items = [text]
        cleaned = [item[:80] for item in items if item]
        return cleaned[:4]

    _BRAND_BEATS = {"open", "mid", "close"}
    _SETPIECE_HOMAGE_TOKENS = (
        "致敬",
        "homage",
        "经典",
        "大片",
        "碟中谍",
        "mission impossible",
        "滑索",
        "杰克逊",
        "moonwalk",
        "滑步",
        "月步",
        "007",
        "james bond",
        "黑客帝国",
        "matrix",
        "夺宝奇兵",
        "indiana",
        "西部",
        "枪管",
        "慢镜",
        "出画",
        "入画",
        "定格",
        "lockup",
    )
    _SETPIECE_HUMOR_TOKENS = (
        "冷幽默",
        "痞笑",
        "痞帅",
        "dry humor",
        "反差",
        "一本正经",
        "rogue smirk",
        "半笑",
        "嘴角",
    )

    @staticmethod
    def _has_any_token(text: str, tokens: tuple) -> bool:
        """文本是否命中任一关键词。"""
        blob = (text or "").lower()
        return any(token.lower() in blob for token in tokens)

    @staticmethod
    def _render_safe_text(value: object) -> str:
        """从模型提示中移除品牌字样，改为后期贴标平面，防止生成乱码。"""
        text = str(value or "").strip()
        return re.sub(
            r"\b(?:Kaka(?:\s+Auto\s+Parts)?|MAXCOOL)\b",
            "approved blank brand patch plane",
            text,
            flags=re.IGNORECASE,
        )

    @classmethod
    def estimate_spoken_seconds(cls, text: str) -> int:
        """按中文短视频可信语速估算口播秒数，并给每个信息点留停顿。"""
        spoken_chars = len(
            re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", str(text or ""))
        )
        if spoken_chars <= 0:
            return 0
        return math.ceil(spoken_chars / cls.NARRATION_CHARS_PER_SECOND) + 1

    @classmethod
    def build_episode_plan(cls, coverage: List[dict]) -> List[dict]:
        """按原稿顺序、真实语速和30秒上限贪心分集。"""
        episodes: List[dict] = []
        current_ids: List[str] = []
        current_seconds = cls.EPISODE_OVERHEAD_SECONDS
        for item in coverage:
            beat_id = str(item.get("beat_id") or "").strip()
            seconds = cls.estimate_spoken_seconds(
                str(item.get("copy_cn_sentence") or "")
            )
            if not beat_id or seconds <= 0:
                raise PipelineError("coverage_check 缺少可估时的中文映射句")
            exceeds_budget = current_seconds + seconds > cls.MAX_EPISODE_SECONDS
            if current_ids and (len(current_ids) >= SCENE_COUNT or exceeds_budget):
                episodes.append(
                    {
                        "episode": len(episodes) + 1,
                        "beat_ids": current_ids,
                        "estimated_seconds": current_seconds,
                    }
                )
                current_ids = []
                current_seconds = cls.EPISODE_OVERHEAD_SECONDS
            if current_seconds + seconds > cls.MAX_EPISODE_SECONDS:
                raise PipelineError(f"{beat_id} 单条文案超过30秒，必须继续拆分信息点")
            current_ids.append(beat_id)
            current_seconds += seconds
        if current_ids:
            episodes.append(
                {
                    "episode": len(episodes) + 1,
                    "beat_ids": current_ids,
                    "estimated_seconds": current_seconds,
                }
            )
        return episodes

    @staticmethod
    def _guard_copy_coverage(raw: dict) -> dict:
        """校验原稿事实账本、完整文案、逐条覆盖与自动分集映射。"""
        source_beats = raw.get("source_beats")
        if not isinstance(source_beats, list) or not source_beats:
            raise PipelineError("视频计划缺少原稿事实账本 source_beats")
        guarded_beats = []
        beat_ids = []
        for index, beat in enumerate(source_beats, start=1):
            if not isinstance(beat, dict):
                raise PipelineError(f"source_beats 第 {index} 条格式错误")
            beat_id = str(beat.get("beat_id") or "").strip()
            source_quote = str(beat.get("source_quote") or "").strip()
            corrected_fact = str(beat.get("corrected_fact") or "").strip()
            evidence_status = str(beat.get("evidence_status") or "").strip()
            if not beat_id or not source_quote or not corrected_fact:
                raise PipelineError(f"source_beats 第 {index} 条缺少原句或校正事实")
            if beat_id in beat_ids:
                raise PipelineError(f"source_beats beat_id 重复: {beat_id}")
            if evidence_status not in {"source_supported", "corrected_asr"}:
                raise PipelineError(f"{beat_id} 缺少原稿证据状态")
            beat_ids.append(beat_id)
            guarded_beats.append(
                {
                    "beat_id": beat_id[:24],
                    "source_quote": source_quote[:500],
                    "corrected_fact": corrected_fact[:300],
                    "evidence_status": evidence_status,
                }
            )

        copy_cn = str(raw.get("copy_cn") or "").strip()
        copy_en = str(raw.get("copy_en") or "").strip()
        if not copy_cn or not copy_en:
            raise PipelineError("必须先产出完整 copy_cn/copy_en 才能写分镜")
        coverage = raw.get("coverage_check")
        if not isinstance(coverage, list):
            raise PipelineError("视频计划缺少 coverage_check")
        guarded_coverage = []
        coverage_ids = []
        for item in coverage:
            if not isinstance(item, dict):
                raise PipelineError("coverage_check 格式错误")
            beat_id = str(item.get("beat_id") or "").strip()
            cn_sentence = str(item.get("copy_cn_sentence") or "").strip()
            en_sentence = str(item.get("copy_en_sentence") or "").strip()
            if beat_id not in beat_ids or beat_id in coverage_ids:
                raise PipelineError("coverage_check 含未知或重复 beat_id")
            if item.get("covered") is not True or not cn_sentence or not en_sentence:
                raise PipelineError(f"{beat_id} 未完整覆盖到中英文文案")
            if item.get("no_new_fact") is not True:
                raise PipelineError(f"{beat_id} 文案新增事实，禁止进入分镜")
            if item.get("no_causal_change") is not True:
                raise PipelineError(f"{beat_id} 文案改变原稿因果，禁止进入分镜")
            if cn_sentence not in copy_cn or en_sentence not in copy_en:
                raise PipelineError(f"{beat_id} 覆盖句未出现在完整文案中")
            coverage_ids.append(beat_id)
            guarded_coverage.append(
                {
                    "beat_id": beat_id,
                    "copy_cn_sentence": cn_sentence[:500],
                    "copy_en_sentence": en_sentence[:700],
                    "covered": True,
                    "no_new_fact": True,
                    "no_causal_change": True,
                }
            )
        if set(coverage_ids) != set(beat_ids):
            missing = sorted(set(beat_ids) - set(coverage_ids))
            raise PipelineError("原稿事实覆盖不完整: " + ",".join(missing))

        expected_episodes = VideoPlanService.build_episode_plan(guarded_coverage)
        expected_count = len(expected_episodes)
        try:
            episode_count = int(raw.get("episode_count") or 0)
            active_episode = int(raw.get("active_episode") or 1)
        except (TypeError, ValueError) as exc:
            raise PipelineError("分集编号无效") from exc
        episode_plan = raw.get("episode_plan")
        if episode_count != expected_count or not isinstance(episode_plan, list):
            raise PipelineError(
                f"{len(beat_ids)} 个信息点必须自动拆为 {expected_count} 集"
            )
        if len(episode_plan) != episode_count or not 1 <= active_episode <= episode_count:
            raise PipelineError("episode_plan 与 episode_count 不一致")
        guarded_episodes = []
        assigned_ids = []
        for expected_episode, episode in enumerate(episode_plan, start=1):
            if not isinstance(episode, dict) or int(episode.get("episode") or 0) != expected_episode:
                raise PipelineError("episode_plan 集号必须连续")
            ids = [str(item).strip() for item in episode.get("beat_ids") or []]
            if not ids or len(ids) > SCENE_COUNT:
                raise PipelineError("每集必须包含 1～4 个原稿信息点")
            if any(item not in beat_ids or item in assigned_ids for item in ids):
                raise PipelineError("episode_plan 含未知或重复信息点")
            assigned_ids.extend(ids)
            expected = expected_episodes[expected_episode - 1]
            if ids != expected["beat_ids"]:
                raise PipelineError(
                    "episode_plan 必须按真实口播语速与30秒上限自动分集"
                )
            guarded_episodes.append(expected)
        if assigned_ids != beat_ids:
            raise PipelineError("episode_plan 必须按原稿顺序覆盖全部信息点")
        return {
            "source_beats": guarded_beats,
            "copy_cn": copy_cn[:10000],
            "copy_en": copy_en[:14000],
            "coverage_check": guarded_coverage,
            "episode_count": episode_count,
            "active_episode": active_episode,
            "episode_plan": guarded_episodes,
        }

    @staticmethod
    def build_render_prompt(plan: dict, scene: dict) -> str:
        """把口播锚点、流程步骤与可核对细节打进生成提示，避免空好看。"""
        details = scene.get("detail_checklist") or []
        if isinstance(details, str):
            details = [details]
        detail_lines = "\n".join(f"- {item}" for item in details) or "- 关键操作面清晰可见"
        brand_beat = str(scene.get("brand_beat") or "mid").strip().lower()
        setpiece_open = VideoPlanService._render_safe_text(plan.get("setpiece_open"))
        setpiece_close = VideoPlanService._render_safe_text(plan.get("setpiece_close"))
        brand_note = {
            "open": (
                "BRAND OPEN + SETPIECE: distinctive cold-humor film homage entrance "
                f"({setpiece_open or 'invent a fresh open setpiece'}); "
                "1–2s then dive into the fault; hold a stable blank chest/hat patch plane "
                "for approved brand compositing later; generate no letters or logo."
            ),
            "mid": (
                "BRAND MID: during the process, let a stable blank cuff / shoulder / "
                "hat-side patch plane enter naturally while hands stay the hero."
            ),
            "close": (
                "BRAND CLOSE + SETPIECE: distinctive cold-humor film homage exit "
                f"({setpiece_close or 'invent a fresh close setpiece'}); "
                "after the teaching beat, rogue smirk (痞帅痞笑), stable blank uniform "
                "lockup plane for approved post-composite branding, clean out."
            ),
        }.get(
            brand_beat,
            "BRAND: reserve a stable blank uniform patch plane for post-compositing.",
        )
        prompt = (
            "Educational automotive commercial, vertical 9:16. "
            "ONE continuous camera move. Structure and process are the hero.\n"
            "PERSON RULES: real-human demeanor, calm and focused; eyes locked on the part "
            "(never vacant/wandering); no exaggerated acting; no long talking-head; "
            "prefer hands / over-shoulder / silhouette; face only as a brief intercalation.\n"
            "BRANDING: approved brand assets are composited later; generate only stable "
            "blank patch planes, never letters, words, captions, or logos.\n"
            f"{brand_note}\n"
            f"TRANSCRIPT ANCHOR (must teach this): {scene.get('transcript_anchor')}\n"
            f"CORE CLAIM: {scene.get('core_claim')}\n"
            f"PROCESS STEP (must be verifiable on screen): {scene.get('process_step')}\n"
            f"STRUCTURE FOCUS: {scene.get('structure_focus')}\n"
            f"CAMERA MOVE: {scene.get('camera_move')}\n"
            f"CHARACTER INSERT: {scene.get('character_insert')}\n"
            "DETAILS THAT MUST SURVIVE slow-motion scrutiny:\n"
            f"{detail_lines}\n"
            "Do NOT invent a different fault, part, or repair path. "
            "No generic garage B-roll that cannot prove the process step. "
            "Do NOT fill the shot with lip-sync talking face. "
            "NO on-screen text, NO captions, NO subtitles, NO generated words or logos. "
            "Brand marks are composited later from approved assets. "
            "No floating fake logo stickers — only real uniform/hat marks. "
            "No copyrighted title cards or celebrity lookalikes of original movie characters.\n"
            f"{VideoPlanService._render_safe_text(scene.get('visual_prompt'))}"
        )
        return VideoPlanService._render_safe_text(prompt)

    @staticmethod
    def guard_plan(raw: dict) -> dict:
        """禁止复用原片；口播核心流程可慢放核对；人物仅穿插。"""
        if not isinstance(raw, dict):
            raise PipelineError("视频计划不是 JSON 对象")
        if raw.get("rights_mode") != "regenerate":
            raise PipelineError("视频计划必须重新生成画面，禁止复用原片")
        copy_package = VideoPlanService._guard_copy_coverage(raw)
        scenes = raw.get("scenes")
        if not isinstance(scenes, list) or len(scenes) != SCENE_COUNT:
            raise PipelineError("视频计划必须恰好包含 4 个场景")
        guarded_scenes = []
        for index, scene in enumerate(scenes, start=1):
            if not isinstance(scene, dict):
                raise PipelineError(f"第 {index} 个场景格式错误")
            try:
                duration = int(scene.get("duration") or 0)
            except (TypeError, ValueError) as exc:
                raise PipelineError(f"第 {index} 个场景时长错误") from exc
            if not MIN_SCENE_DURATION_SECONDS <= duration <= MAX_SCENE_DURATION_SECONDS:
                raise PipelineError("每个场景必须为 4～15 秒")
            visual_prompt = str(scene.get("visual_prompt") or "").strip()
            narration = str(scene.get("narration") or "").strip()
            narration_en = str(scene.get("narration_en") or "").strip()
            if not visual_prompt or not narration:
                raise PipelineError(f"第 {index} 个场景缺少画面或旁白")
            if not narration_en:
                raise PipelineError(f"第 {index} 个场景缺少海外英文旁白")
            if len(visual_prompt) > 4000:
                # 入出场 setpiece 会拉长描写；超长时保留头尾关键句，避免整单失败
                visual_prompt = (
                    visual_prompt[:3200].rstrip()
                    + " … "
                    + visual_prompt[-600:].lstrip()
                )
            if (
                len(narration) > 100
                or len(narration_en) > 140
            ):
                raise PipelineError(f"第 {index} 个场景文字过长")
            # 粗拦明显直译腔 / 空钩子（不求完美 NLP）
            if any(
                bad in narration_en.lower()
                for bad in ("must see", "amazing!!!", "cure all", "100% fix")
            ):
                raise PipelineError(f"第 {index} 个场景英文旁白不够可信，避免吆喝腔")
            unsafe_hits = []
            for token in ("复用原片", "裁剪原片", "source_path", "source_video"):
                if token in visual_prompt:
                    unsafe_hits.append(token)
            # 「无原片人物 / 禁止原片人物」是否定写法，不算复用
            if "原片人物" in visual_prompt and not any(
                neg in visual_prompt
                for neg in ("无原片人物", "禁止原片人物", "不含原片人物", "不是原片人物")
            ):
                unsafe_hits.append("原片人物")
            if unsafe_hits:
                raise PipelineError("视频计划不得引用或复用原片")
            beat = str(scene.get("beat") or "").strip()[:120]
            brand_beat = str(scene.get("brand_beat") or "").strip().lower()[:20]
            structure_focus = str(scene.get("structure_focus") or "").strip()[:160]
            transcript_anchor = str(scene.get("transcript_anchor") or "").strip()[:200]
            character_insert = str(scene.get("character_insert") or "").strip()[:160]
            camera_move = str(scene.get("camera_move") or "").strip()[:120]
            process_step = str(scene.get("process_step") or "").strip()[:160]
            core_claim = str(scene.get("core_claim") or "").strip()[:160]
            detail_checklist = VideoPlanService._normalize_checklist(
                scene.get("detail_checklist")
            )
            scene_beat_ids = [
                str(item).strip() for item in scene.get("beat_ids") or []
            ]
            active_beat_ids = set(
                copy_package["episode_plan"][
                    copy_package["active_episode"] - 1
                ]["beat_ids"]
            )
            if not scene_beat_ids or any(
                item not in active_beat_ids for item in scene_beat_ids
            ):
                raise PipelineError(
                    f"第 {index} 个场景 beat_ids 必须来自当前分集原稿"
                )
            if not structure_focus:
                raise PipelineError(f"第 {index} 个场景缺少结构重建重点")
            if not transcript_anchor:
                raise PipelineError(f"第 {index} 个场景缺少口播锚点 transcript_anchor")
            if not camera_move:
                raise PipelineError(f"第 {index} 个场景缺少连续运镜 camera_move")
            if not character_insert:
                raise PipelineError(f"第 {index} 个场景缺少人物穿插方式")
            if not process_step:
                raise PipelineError(f"第 {index} 个场景缺少可核对流程 process_step")
            if not core_claim:
                raise PipelineError(f"第 {index} 个场景缺少核心教学判断 core_claim")
            if brand_beat not in VideoPlanService._BRAND_BEATS:
                raise PipelineError(
                    f"第 {index} 个场景 brand_beat 必须是 open/mid/close（品牌前中后）"
                )
            if len(detail_checklist) < 2:
                raise PipelineError(
                    f"第 {index} 个场景 detail_checklist 至少 2 条可慢放核对的细节"
                )
            brand_blob = f"{visual_prompt}\n{beat}\n{brand_beat}"
            if brand_beat in {"open", "close"} and not any(
                token in brand_blob.lower()
                for token in ("kaka", "maxcool", "胸标", "帽", "工服", "标识", "lockup", "brand")
            ):
                raise PipelineError(
                    f"第 {index} 个场景（{brand_beat}）画面需写出品牌落点，不能空镜"
                )
            if brand_beat in {"open", "close"} and not (
                VideoPlanService._has_any_token(
                    brand_blob, VideoPlanService._SETPIECE_HOMAGE_TOKENS
                )
                and VideoPlanService._has_any_token(
                    brand_blob, VideoPlanService._SETPIECE_HUMOR_TOKENS
                )
            ):
                raise PipelineError(
                    f"第 {index} 个场景（{brand_beat}）须含冷幽默 + 经典大片致敬入/出场"
                )
            matched = next(
                (
                    mode
                    for mode in VideoPlanService._INSERT_MODES
                    if mode in character_insert
                ),
                "",
            )
            if not matched:
                raise PipelineError(
                    f"第 {index} 个场景人物穿插必须是 "
                    "cutaway_host/over_shoulder/hand_insert/silhouette_pass"
                )
            blob = f"{visual_prompt}\n{structure_focus}\n{process_step}".lower()
            if not any(token in blob for token in VideoPlanService._STRUCTURE_TOKENS):
                raise PipelineError(f"第 {index} 个场景必须以汽车结构与操作为主体")
            # 流程词只看 process_step，避免 visual 里残留动词掩盖空步骤
            if not any(
                token in process_step
                for token in (
                    "拔",
                    "插",
                    "拆",
                    "装",
                    "查",
                    "看",
                    "对比",
                    "启动",
                    "清洗",
                    "复检",
                    "指向",
                    "按",
                    "拧",
                    "测量",
                    "观察",
                    "验证",
                    "unplug",
                    "reconnect",
                    "compare",
                    "inspect",
                    "point",
                    "measure",
                    "verify",
                )
            ):
                raise PipelineError(f"第 {index} 个场景 process_step 必须是可执行动作")
            guarded_scenes.append(
                {
                    "duration": duration,
                    "beat": beat,
                    "brand_beat": brand_beat,
                    "transcript_anchor": transcript_anchor,
                    "core_claim": core_claim,
                    "process_step": process_step,
                    "structure_focus": structure_focus,
                    "detail_checklist": detail_checklist,
                    "character_insert": character_insert[:160],
                    "camera_move": camera_move,
                    "visual_prompt": visual_prompt,
                    "beat_ids": scene_beat_ids,
                    "narration": narration,
                    "narration_en": narration_en,
                    "_insert_mode": matched,
                }
            )
        scene_beat_ids = {
            beat_id
            for scene in guarded_scenes
            for beat_id in scene["beat_ids"]
        }
        expected_scene_beats = set(
            copy_package["episode_plan"][
                copy_package["active_episode"] - 1
            ]["beat_ids"]
        )
        if scene_beat_ids != expected_scene_beats:
            raise PipelineError("当前分集 scenes 未覆盖该集全部原稿信息点")
        active_episode_seconds = copy_package["episode_plan"][
            copy_package["active_episode"] - 1
        ]["estimated_seconds"]
        rendered_seconds = sum(scene["duration"] for scene in guarded_scenes)
        if rendered_seconds < active_episode_seconds:
            raise PipelineError(
                "当前分集镜头总时长短于真实口播估时，禁止为凑时长删减文案"
            )
        host_cuts = sum(
            1 for scene in guarded_scenes if scene.get("_insert_mode") == "cutaway_host"
        )
        if host_cuts > 1:
            raise PipelineError("整片最多 1 段以 cutaway_host 正脸讲解，降低露怯")
        brand_beats = {scene["brand_beat"] for scene in guarded_scenes}
        missing_beats = VideoPlanService._BRAND_BEATS - brand_beats
        if missing_beats:
            raise PipelineError(
                "品牌植入必须覆盖前中后（open/mid/close），缺少: "
                + ",".join(sorted(missing_beats))
            )
        for scene in guarded_scenes:
            scene.pop("_insert_mode", None)
        title = str(raw.get("title") or "").strip()[:120]
        title_en = str(raw.get("title_en") or "").strip()[:140]
        hook = str(raw.get("hook") or "").strip()[:120]
        hook_en = str(raw.get("hook_en") or "").strip()[:140]
        character_lock = str(raw.get("character_lock") or "").strip()[:300]
        look = str(raw.get("look") or "").strip()[:200]
        look_variant = str(raw.get("look_variant") or "no_hat").strip()[:40]
        direction = str(raw.get("direction") or "").strip()[:220]
        core_arc = str(raw.get("core_arc") or "").strip()[:220]
        if not title or not hook:
            raise PipelineError("视频计划缺少标题或开头钩子")
        if not title_en or not hook_en:
            raise PipelineError("视频计划缺少海外英文标题或钩子")
        if "品牌本人" not in character_lock or "年轻化" not in character_lock:
            raise PipelineError("视频计划必须锁定品牌本人年轻化形象")
        if "眼神" not in character_lock and "专注" not in character_lock:
            raise PipelineError("人物锁定必须要求专注眼神，禁止游离夸张")
        branding = str(raw.get("branding") or "").strip()[:200]
        setpiece_open = str(raw.get("setpiece_open") or "").strip()[:220]
        setpiece_close = str(raw.get("setpiece_close") or "").strip()[:220]
        branding_blob = f"{character_lock}\n{branding}"
        if "Kaka Auto Parts" not in branding_blob or "MAXCOOL" not in branding_blob:
            raise PipelineError("工服与帽子必须带 Kaka Auto Parts 与 MAXCOOL 标识")
        brand_arc_blob = f"{branding}\n{direction}\n{look}".lower()
        if not any(
            token in brand_arc_blob
            for token in ("前中后", "open", "mid", "close", "前", "中", "后", "lockup")
        ):
            raise PipelineError("branding/direction 须写明品牌前中后植入")
        if not setpiece_open or not setpiece_close:
            raise PipelineError("必须设计独特入场 setpiece_open 与出场 setpiece_close")
        if setpiece_open == setpiece_close:
            raise PipelineError("入场与出场 setpiece 不能雷同")
        for label, piece in (("入场", setpiece_open), ("出场", setpiece_close)):
            if not (
                VideoPlanService._has_any_token(
                    piece, VideoPlanService._SETPIECE_HOMAGE_TOKENS
                )
                and VideoPlanService._has_any_token(
                    piece, VideoPlanService._SETPIECE_HUMOR_TOKENS
                )
            ):
                raise PipelineError(f"{label} setpiece 须含冷幽默与经典大片致敬")
        if look_variant not in {"no_hat", "with_hat"}:
            raise PipelineError("造型只允许 no_hat 或 with_hat")
        look_blob = f"{direction}\n{look}"
        if "一镜到底" not in look_blob and "连续" not in look_blob:
            raise PipelineError("成片方向必须强调一镜到底或连续运镜")
        if not core_arc:
            raise PipelineError("计划必须给出口播核心链路 core_arc")
        return {
            "title": title,
            "title_en": title_en,
            "hook": hook,
            "hook_en": hook_en,
            "rights_mode": "regenerate",
            "direction": direction
            or "一镜到底商业广告科普：还原口播；品牌前中后；入出场冷幽默大片致敬",
            "core_arc": core_arc,
            "character_lock": character_lock,
            "look_variant": look_variant,
            "look": look
            or "一镜到底感商业广告汽修科普，侧逆光；入出场特色 setpiece，竖屏 9:16",
            "branding": branding
            or "Kaka Auto Parts + MAXCOOL；前开场露出、中段操作穿插、末段 lockup + 痞帅痞笑",
            "setpiece_open": setpiece_open,
            "setpiece_close": setpiece_close,
            **copy_package,
            "scenes": guarded_scenes,
            "duration_seconds": rendered_seconds,
        }


class AdsMediaGatewayClient:
    """通过公司网关调用 Wan 和 CosyVoice，客户端不持有模型密钥。"""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.username = username
        self.password = password
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            transport=transport,
            follow_redirects=False,
            timeout=90,
        )
        self.logged_in = False

    def _login(self) -> None:
        """登录一次并复用会话。"""
        if self.logged_in:
            return
        if not self.username or not self.password:
            raise PipelineError("公司 AI 媒体账号尚未配置")
        try:
            response = self.client.post(
                "/login",
                data={"username": self.username, "password": self.password},
            )
        except httpx.HTTPError as exc:
            raise PipelineError("公司 AI 媒体网关连接失败") from exc
        if response.status_code != 302 or "session_token" not in self.client.cookies:
            raise PipelineError("公司 AI 媒体网关登录失败")
        self.logged_in = True

    @staticmethod
    def _payload(response: httpx.Response, action: str) -> dict:
        """统一解析安全 JSON 响应。"""
        if response.status_code != 200:
            try:
                detail = response.json().get("error") or ""
            except (json.JSONDecodeError, TypeError):
                detail = ""
            raise PipelineError(detail or f"{action}失败：HTTP {response.status_code}")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise PipelineError(f"{action}返回格式错误") from exc
        if not isinstance(payload, dict):
            raise PipelineError(f"{action}未返回 JSON 对象")
        return payload

    def quote(
        self,
        quality: str,
        generation_id: str = "",
        scene_durations: Optional[List[int]] = None,
    ) -> dict:
        """读取媒体网关当前模型的精确整条视频报价。"""
        self._login()
        durations_param = ",".join(
            str(value) for value in (scene_durations or [])
        )
        try:
            response = self.client.get(
                "/api/content-index/video/quote",
                params={
                    "quality": quality,
                    "generation_id": generation_id,
                    "scene_durations": durations_param,
                },
            )
        except httpx.HTTPError as exc:
            raise PipelineError("视频报价网络失败") from exc
        quote = self._payload(response, "视频报价")
        try:
            returned_durations = [
                int(value) for value in quote.get("scene_durations") or []
            ]
            expected_durations = scene_durations or [
                LEGACY_GATEWAY_SCENE_DURATION_SECONDS
            ] * SCENE_COUNT
            valid = (
                quote.get("quality") == quality
                and int(quote.get("scene_count")) == SCENE_COUNT
                and returned_durations == expected_durations
                and float(quote.get("total_cost_cny")) > 0
                and len(str(quote.get("quote_id") or "")) == 64
                and len(str(quote.get("generation_id") or "")) == 32
                and bool(str(quote.get("model") or ""))
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise PipelineError("视频报价内容无效")
        return quote

    def upload_reference(
        self,
        clip_path: Path,
        *,
        generation_id: str,
        scene_index: int,
    ) -> str:
        """上传经授权的短参考片段，只接收网关返回的私有对象键。"""
        self._login()
        try:
            data = clip_path.read_bytes()
            response = self.client.post(
                "/api/content-index/video/reference",
                params={
                    "generation_id": generation_id,
                    "scene_index": scene_index,
                    "rights_confirmed": "true",
                },
                content=data,
                headers={"content-type": "video/mp4"},
            )
        except (OSError, httpx.HTTPError) as exc:
            raise PipelineError("参考片段上传失败") from exc
        payload = self._payload(response, "参考片段上传")
        key = str(payload.get("reference_key") or "")
        if not key.startswith("ai-reference/") or not key.endswith(".mp4"):
            raise PipelineError("参考片段对象键无效")
        return key

    def submit_scene(
        self,
        prompt: str,
        duration: int,
        quality: str,
        quote_id: str,
        generation_id: str,
        scene_index: int,
        look_variant: str = "no_hat",
        scene_durations: Optional[List[int]] = None,
        reference_key: str = "",
    ) -> dict:
        """提交付费视频场景；调用方必须已取得本次费用确认。"""
        self._login()
        body = {
            "prompt": prompt,
            "duration": duration,
            "quality": quality,
            "cost_confirmed": True,
            "quote_id": quote_id,
            "generation_id": generation_id,
            "scene_index": scene_index,
            "look_variant": look_variant,
        }
        if scene_durations is not None:
            body["scene_durations"] = scene_durations
        if reference_key:
            body["reference_key"] = reference_key
        try:
            response = self.client.post(
                "/api/content-index/video/submit",
                json=body,
            )
        except httpx.HTTPError as exc:
            raise PipelineError("视频任务提交网络失败") from exc
        return self._payload(response, "视频任务提交")

    def get_task(self, task_id: str) -> dict:
        """查询一个 Wan 视频任务。"""
        self._login()
        try:
            response = self.client.get(
                f"/api/content-index/video/tasks/{task_id}",
            )
        except httpx.HTTPError as exc:
            raise PipelineError("视频任务查询网络失败") from exc
        return self._payload(response, "视频任务查询")

    def synthesize_voice(self, text: str) -> dict:
        """把重写旁白发送给 CosyVoice。"""
        self._login()
        try:
            response = self.client.post(
                "/api/content-index/video/tts",
                json={"text": text},
            )
        except httpx.HTTPError as exc:
            raise PipelineError("AI 配音网络失败") from exc
        return self._payload(response, "AI 配音")

    def close(self) -> None:
        """释放 HTTP 连接。"""
        self.client.close()


def _srt_timestamp(seconds: int) -> str:
    """把整数秒转换成 SRT 时间戳。"""
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},000"


class LocalVideoRenderer:
    """使用本机 FFmpeg 拼接 AI 镜头、配音并烧录字幕。"""

    def __init__(self, ffmpeg_path: str):
        self.ffmpeg_path = ffmpeg_path

    @staticmethod
    def _run(command: List[str]) -> None:
        """执行一条有界 FFmpeg 命令。"""
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PipelineError("本机视频合成程序无法启动") from exc
        if result.returncode != 0:
            raise PipelineError("本机视频合成失败：" + (result.stderr or "")[-300:])

    def render(
        self,
        *,
        source_id: str,
        plan: dict,
        scene_paths: List[Path],
        voice_path: Path,
        output_root: Path,
    ) -> Path:
        """合成竖屏成片；参数中不存在参考原片路径。"""
        if len(scene_paths) != SCENE_COUNT or any(not path.is_file() for path in scene_paths):
            raise PipelineError("AI 视频片段不完整")
        if not voice_path.is_file():
            raise PipelineError("AI 配音文件不存在")
        target = Path(output_root) / source_id
        target.mkdir(parents=True, exist_ok=True)
        concat_file = target / "scenes.txt"
        concat_file.write_text(
            "\n".join(f"file '{path.resolve()}'" for path in scene_paths) + "\n",
            encoding="utf-8",
        )
        montage = target / "montage.mp4"
        final = target / "final.mp4"
        self._run(
            [
                self.ffmpeg_path,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(montage),
            ]
        )
        self._run(
            [
                self.ffmpeg_path,
                "-y",
                "-i",
                str(montage),
                "-i",
                str(voice_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-shortest",
                "-movflags",
                "+faststart",
                str(final),
            ]
        )
        return final
