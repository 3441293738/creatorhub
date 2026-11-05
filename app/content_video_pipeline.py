"""参考内容转全新 AI 视频的本机计划、网关和渲染组件。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

from .content_pipeline import JSONCaller, PROMPT_ROOT, PipelineError


SCENE_COUNT = 4
SCENE_DURATION_SECONDS = 5
QUALITY_COST_CNY_PER_SECOND = {
    "preview": 0.24,
    "quality": 0.60,
}


def estimate_plan_cost(plan: dict, quality: str) -> float:
    """按模型公开单价估算视频费用，不包含极低的文字与配音费用。"""
    if quality not in QUALITY_COST_CNY_PER_SECOND:
        raise PipelineError("未知视频质量档位")
    duration = sum(int(scene.get("duration") or 0) for scene in plan.get("scenes") or [])
    return round(duration * QUALITY_COST_CNY_PER_SECOND[quality], 2)


class VideoPlanService:
    """用文字模型把参考口播改写成安全的全新视频分镜。"""

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

    @staticmethod
    def guard_plan(raw: dict) -> dict:
        """禁止复用原片，并把时长和字段锁定在可渲染范围。"""
        if not isinstance(raw, dict):
            raise PipelineError("视频计划不是 JSON 对象")
        if raw.get("rights_mode") != "regenerate":
            raise PipelineError("视频计划必须重新生成画面，禁止复用原片")
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
            if duration != SCENE_DURATION_SECONDS:
                raise PipelineError("每个场景必须固定 5 秒")
            visual_prompt = str(scene.get("visual_prompt") or "").strip()
            narration = str(scene.get("narration") or "").strip()
            subtitle = str(scene.get("subtitle") or "").strip()
            if not visual_prompt or not narration or not subtitle:
                raise PipelineError(f"第 {index} 个场景缺少画面、旁白或字幕")
            if len(visual_prompt) > 800 or len(narration) > 80 or len(subtitle) > 40:
                raise PipelineError(f"第 {index} 个场景文字过长")
            unsafe = ("复用原片", "裁剪原片", "source_path", "source_video")
            if any(token in visual_prompt for token in unsafe):
                raise PipelineError("视频计划不得引用或复用原片")
            guarded_scenes.append(
                {
                    "duration": duration,
                    "visual_prompt": visual_prompt,
                    "narration": narration,
                    "subtitle": subtitle,
                }
            )
        title = str(raw.get("title") or "").strip()[:120]
        hook = str(raw.get("hook") or "").strip()[:120]
        if not title or not hook:
            raise PipelineError("视频计划缺少标题或开头钩子")
        return {
            "title": title,
            "hook": hook,
            "rights_mode": "regenerate",
            "scenes": guarded_scenes,
            "duration_seconds": SCENE_COUNT * SCENE_DURATION_SECONDS,
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

    def submit_scene(self, prompt: str, duration: int, quality: str) -> dict:
        """提交付费视频场景；调用方必须已取得本次费用确认。"""
        self._login()
        try:
            response = self.client.post(
                "/api/content-index/video/submit",
                json={
                    "prompt": prompt,
                    "duration": duration,
                    "quality": quality,
                    "cost_confirmed": True,
                },
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
        subtitles = target / "subtitles.srt"
        lines = []
        cursor = 0
        for index, scene in enumerate(plan["scenes"], start=1):
            end = cursor + int(scene["duration"])
            lines.extend(
                [
                    str(index),
                    f"{_srt_timestamp(cursor)} --> {_srt_timestamp(end)}",
                    str(scene["subtitle"]),
                    "",
                ]
            )
            cursor = end
        subtitles.write_text("\n".join(lines), encoding="utf-8")
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
        escaped_subtitles = str(subtitles).replace("\\", "\\\\").replace(":", "\\:")
        self._run(
            [
                self.ffmpeg_path,
                "-y",
                "-i",
                str(montage),
                "-i",
                str(voice_path),
                "-vf",
                f"subtitles={escaped_subtitles}",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                "-shortest",
                "-movflags",
                "+faststart",
                str(final),
            ]
        )
        return final
