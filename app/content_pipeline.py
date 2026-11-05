"""本机成品索引自动流水线。

原视频和完整口播只留在本机。AI 只接收口播文字；共享同步只导出元数据和草稿。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple

import httpx

log = logging.getLogger(__name__)

PLATFORMS = (
    "douyin",
    "tiktok",
    "youtube",
    "facebook",
    "x",
    "reddit",
    "pinterest",
    "quora",
    "linkedin",
)
TOPICS = ("维修", "养护", "车型对比", "故障诊断", "零件知识", "用车技巧", "其他")
MAKES = ("Toyota", "Honda", "Nissan")
MAKE_ALIASES = {
    "Toyota": ("toyota", "丰田"),
    "Honda": ("honda", "本田"),
    "Nissan": ("nissan", "日产"),
}
VIDEO_ID_RE = re.compile(r"^(\d{6,})")
YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
PROMPT_ROOT = Path(__file__).with_name("prompts")


class PipelineError(ValueError):
    """自动流水线可向小白展示的错误。"""


class JSONCaller(Protocol):
    """结构化 AI 调用协议。"""

    def call_json(self, prompt: str) -> Tuple[dict, str]:
        """返回 JSON 对象和实际模型通道。"""


@dataclass(frozen=True)
class SourceItem:
    """本机一条已精确关联的原片和口播。"""

    source_id: str
    author: str
    source_title: str
    source_path: str
    transcript_path: str
    transcript: str
    sha256: str
    size_bytes: int
    mtime: int

    def asset_record(self) -> dict:
        """转为本机索引记录。"""
        return {
            "source_id": self.source_id,
            "author": self.author,
            "source_title": self.source_title,
            "source_path": self.source_path,
            "transcript_path": self.transcript_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mtime": self.mtime,
        }


@dataclass(frozen=True)
class ScanReport:
    """本机扫描结果。"""

    items: List[SourceItem]
    counts: Dict[str, int]
    pending: List[dict]


def _sha256(path: Path) -> str:
    """流式计算文件指纹，不修改文件。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _video_id(name: str) -> str:
    """从文件名开头提取权威视频编号。"""
    match = VIDEO_ID_RE.match(name)
    if not match:
        raise PipelineError(f"文件名没有视频编号：{name}")
    return match.group(1)


def scan_sources(share_root: Path, inbox_root: Path) -> ScanReport:
    """精确关联 share 原片和 inbox 口播；歧义项绝不自动猜。"""
    grouped: Dict[str, List[Path]] = {}
    for path in sorted(Path(share_root).rglob("*.mp4")):
        grouped.setdefault(_video_id(path.name), []).append(path)
    transcripts = {
        path.parent.name: path
        for path in sorted(Path(inbox_root).glob("*/transcript.txt"))
    }
    items: List[SourceItem] = []
    pending: List[dict] = []
    for source_id, paths in sorted(grouped.items()):
        transcript_path = transcripts.get(source_id)
        if len(paths) != 1 or transcript_path is None:
            pending.append({
                "source_id": source_id,
                "video_count": len(paths),
                "has_transcript": transcript_path is not None,
            })
            continue
        path = paths[0]
        stat = path.stat()
        title = path.stem.split("_", 1)[1] if "_" in path.stem else path.stem
        items.append(SourceItem(
            source_id=source_id,
            author=path.parent.name,
            source_title=title,
            source_path=str(path),
            transcript_path=str(transcript_path),
            transcript=transcript_path.read_text(encoding="utf-8").strip(),
            sha256=_sha256(path),
            size_bytes=stat.st_size,
            mtime=stat.st_mtime_ns,
        ))
    counts = {
        "video": sum(len(paths) for paths in grouped.values()),
        "transcript": len(transcripts),
        "matched": len(items),
        "missing": len(pending),
    }
    return ScanReport(items=items, counts=counts, pending=pending)


def resolve_secret(name: str) -> str:
    """从进程环境或系统凭证库读取密钥，不读取项目配置文件。"""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "CreatorHub", "-a", name, "-w"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.SubprocessError, OSError):
            return ""
        return result.stdout.strip()
    return ""


class OpenAICompatibleJSONClient:
    """DeepSeek 首选、百炼 Qwen 备用的 JSON 客户端。"""

    def __init__(self, timeout: float = 65.0):
        self.timeout = timeout

    @staticmethod
    def _providers() -> List[dict]:
        return [
            {
                "name": "deepseek",
                "key": resolve_secret("DEEPSEEK_API_KEY"),
                "url": "https://api.deepseek.com/v1/chat/completions",
                "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            },
            {
                "name": "qwen",
                "key": resolve_secret("CODING_PLAN_API_KEY") or resolve_secret("DASHSCOPE_API_KEY"),
                "url": (
                    os.environ.get(
                        "CODING_PLAN_BASE_URL",
                        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                    ).rstrip("/")
                    + "/chat/completions"
                ),
                "model": os.environ.get("CODING_PLAN_MAIN_MODEL", "qwen3.5-plus"),
            },
        ]

    def call_json(self, prompt: str) -> Tuple[dict, str]:
        """调用首个可用模型；失败自动回退，不记录密钥和完整口播。"""
        configured = [provider for provider in self._providers() if provider["key"]]
        if not configured:
            raise PipelineError(
                "AI 凭证未配置。请把 DEEPSEEK_API_KEY 和 DASHSCOPE_API_KEY 存入系统凭证库。"
            )
        errors = []
        for provider in configured:
            try:
                response = httpx.post(
                    provider["url"],
                    headers={"Authorization": f"Bearer {provider['key']}"},
                    json={
                        "model": provider["model"],
                        "temperature": 0.1,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {
                                "role": "system",
                                "content": "只输出有效 JSON，不要 Markdown，不得猜测没有证据的事实。",
                            },
                            {"role": "user", "content": prompt},
                        ],
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                parsed = _parse_json_object(content)
                log.info("content AI completed provider=%s", provider["name"])
                return parsed, provider["name"]
            except (
                httpx.HTTPError,
                KeyError,
                IndexError,
                TypeError,
                json.JSONDecodeError,
                PipelineError,
            ) as exc:
                errors.append(f"{provider['name']}:{type(exc).__name__}")
                log.warning("content AI failed provider=%s err=%s", provider["name"], type(exc).__name__)
        raise PipelineError("AI 自动处理失败，已尝试：" + "、".join(errors))


def _parse_json_object(content: str) -> dict:
    """容忍模型偶发的 Markdown 包裹，但只接受 JSON 对象。"""
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise PipelineError("AI 返回的不是 JSON 对象")
    return parsed


def _prompt(name: str) -> str:
    """读取随客户端发布的提示词。"""
    path = PROMPT_ROOT / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PipelineError(f"缺少 AI 提示词：{name}") from exc


class AIContentService:
    """AI 元数据与九平台草稿服务。"""

    def __init__(self, caller: JSONCaller):
        self.caller = caller

    def analyze(self, item: SourceItem) -> Tuple[dict, str]:
        """根据口播生成元数据，并执行证据门禁。"""
        prompt = _prompt("content_metadata.txt").replace("{{transcript}}", item.transcript)
        raw, provider = self.caller.call_json(prompt)
        return self.guard_metadata(raw, item.transcript), provider

    def make_drafts(self, metadata: dict, transcript: str) -> Tuple[dict, str]:
        """一次生成九个平台的独立字段化草稿。"""
        prompt = (
            _prompt("platform_drafts.txt")
            .replace("{{metadata}}", json.dumps(metadata, ensure_ascii=False))
            .replace("{{transcript}}", transcript)
        )
        raw, provider = self.caller.call_json(prompt)
        return self.guard_drafts(raw), provider

    @staticmethod
    def guard_metadata(raw: dict, transcript: str) -> dict:
        """没有口播证据的 Make/Model/Year 必须清空并转人工复核。"""
        result = {
            "title": str(raw.get("title") or "").strip()[:120],
            "summary": str(raw.get("summary") or "").strip()[:1000],
            "content_type": str(raw.get("content_type") or "知识讲解").strip()[:40],
            "make": str(raw.get("make") or "").strip(),
            "model": str(raw.get("model") or "").strip(),
            "year_start": raw.get("year_start"),
            "year_end": raw.get("year_end"),
            "topic": raw.get("topic") if raw.get("topic") in TOPICS else "其他",
            "confidence": max(0.0, min(1.0, float(raw.get("confidence") or 0))),
            "evidence": [str(value)[:160] for value in (raw.get("evidence") or [])[:5]],
        }
        unsupported = False
        transcript_lower = transcript.lower()
        make = result["make"]
        if make:
            canonical = next(
                (name for name, aliases in MAKE_ALIASES.items() if make.lower() in aliases),
                "",
            )
            if not canonical or not any(alias in transcript_lower for alias in MAKE_ALIASES[canonical]):
                result["make"] = ""
                unsupported = True
            else:
                result["make"] = canonical
        if result["model"] and result["model"].lower() not in transcript_lower:
            result["model"] = ""
            unsupported = True
        years_in_text = {int(value) for value in YEAR_RE.findall(transcript)}
        for key in ("year_start", "year_end"):
            try:
                year = int(result[key]) if result[key] is not None else None
            except (TypeError, ValueError):
                year = None
            if year is not None and year not in years_in_text:
                unsupported = True
                year = None
            result[key] = year
        result["review_required"] = unsupported or result["confidence"] < 0.75
        result["evidence_scope"] = "transcript_only"
        if not result["title"] or not result["summary"]:
            raise PipelineError("AI 未生成标题或摘要")
        return result

    @staticmethod
    def guard_drafts(raw: dict) -> dict:
        """校验九个平台恰好各一份，避免复制同一稿。"""
        missing = [platform for platform in PLATFORMS if not isinstance(raw.get(platform), dict)]
        if missing:
            raise PipelineError("缺少平台草稿：" + "、".join(missing))
        drafts = {}
        bodies = []
        for platform in PLATFORMS:
            fields = dict(raw[platform])
            title = str(fields.get("title") or "").strip()
            body = str(fields.get("body") or "").strip()
            if not title or not body:
                raise PipelineError(f"{platform} 缺少标题或正文")
            fields["title"] = title
            fields["body"] = body
            fields["language"] = str(fields.get("language") or "")
            fields["notes"] = str(fields.get("notes") or "")
            fields["hashtags"] = list(fields.get("hashtags") or [])
            drafts[platform] = fields
            bodies.append(body)
        if len(set(bodies)) != len(PLATFORMS):
            raise PipelineError("九个平台存在完全相同的正文")
        return drafts


class ContentIndexStore:
    """独立 SQLite 缓存，不写 CreatorHub 原业务数据库。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS assets (
                    source_id TEXT PRIMARY KEY,
                    author TEXT NOT NULL,
                    source_title TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    transcript_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime INTEGER NOT NULL,
                    metadata_json TEXT,
                    metadata_model TEXT,
                    metadata_confirmed INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'indexed',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS drafts (
                    source_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    fields_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    model TEXT NOT NULL,
                    rule_version TEXT NOT NULL DEFAULT 'v1',
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, platform)
                );
                CREATE TABLE IF NOT EXISTS video_jobs (
                    source_id TEXT PRIMARY KEY,
                    plan_json TEXT NOT NULL,
                    plan_model TEXT NOT NULL,
                    quality TEXT NOT NULL DEFAULT 'preview',
                    status TEXT NOT NULL DEFAULT 'planned',
                    progress INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_cny REAL NOT NULL DEFAULT 0,
                    quote_id TEXT NOT NULL DEFAULT '',
                    quote_model TEXT NOT NULL DEFAULT '',
                    generation_id TEXT NOT NULL DEFAULT '',
                    output_path TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(video_jobs)").fetchall()
            }
            if "quote_id" not in columns:
                conn.execute(
                    "ALTER TABLE video_jobs ADD COLUMN quote_id TEXT NOT NULL DEFAULT ''"
                )
            if "quote_model" not in columns:
                conn.execute(
                    "ALTER TABLE video_jobs ADD COLUMN quote_model TEXT NOT NULL DEFAULT ''"
                )
            if "generation_id" not in columns:
                conn.execute(
                    "ALTER TABLE video_jobs ADD COLUMN generation_id TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                """
                UPDATE video_jobs SET status='failed', progress=0,
                    error='旧任务缺少服务端报价，请重新确认费用后再制作'
                WHERE status IN ('queued', 'generating', 'rendering')
                    AND (quote_id='' OR generation_id='')
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def upsert_asset(self, asset: dict) -> None:
        """按视频编号幂等更新本机文件指纹。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO assets (
                    source_id, author, source_title, source_path, transcript_path,
                    sha256, size_bytes, mtime, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    author=excluded.author, source_title=excluded.source_title,
                    source_path=excluded.source_path, transcript_path=excluded.transcript_path,
                    sha256=excluded.sha256, size_bytes=excluded.size_bytes,
                    mtime=excluded.mtime, updated_at=excluded.updated_at
                """,
                (
                    asset["source_id"],
                    asset["author"],
                    asset["source_title"],
                    asset["source_path"],
                    asset["transcript_path"],
                    asset["sha256"],
                    int(asset["size_bytes"]),
                    int(asset["mtime"]),
                    self._now(),
                ),
            )

    def save_metadata(self, source_id: str, metadata: dict, model: str, *, confirmed: bool) -> None:
        """保存 AI 建议；已人工确认的值不会被 AI 静默覆盖。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT metadata_confirmed FROM assets WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if not row:
                raise PipelineError(f"本机索引不存在：{source_id}")
            if row["metadata_confirmed"] and not confirmed:
                return
            conn.execute(
                """
                UPDATE assets SET metadata_json=?, metadata_model=?,
                    metadata_confirmed=?, status='drafted', error='', updated_at=?
                WHERE source_id=?
                """,
                (
                    json.dumps(metadata, ensure_ascii=False),
                    model,
                    1 if confirmed else 0,
                    self._now(),
                    source_id,
                ),
            )

    def save_drafts(self, source_id: str, drafts: dict, model: str) -> None:
        """幂等保存九个平台草稿；已进入审核的稿件不自动覆盖。"""
        with self._connect() as conn:
            for platform, fields in drafts.items():
                row = conn.execute(
                    "SELECT status, version FROM drafts WHERE source_id=? AND platform=?",
                    (source_id, platform),
                ).fetchone()
                if row and row["status"] != "draft":
                    continue
                version = int(row["version"]) + 1 if row else 1
                conn.execute(
                    """
                    INSERT INTO drafts (
                        source_id, platform, fields_json, status, model, version, updated_at
                    ) VALUES (?, ?, ?, 'draft', ?, ?, ?)
                    ON CONFLICT(source_id, platform) DO UPDATE SET
                        fields_json=excluded.fields_json, model=excluded.model,
                        version=excluded.version, updated_at=excluded.updated_at
                    """,
                    (
                        source_id,
                        platform,
                        json.dumps(fields, ensure_ascii=False),
                        model,
                        version,
                        self._now(),
                    ),
                )

    def save_video_plan(
        self,
        source_id: str,
        plan: dict,
        model: str,
        *,
        estimated_cost_cny: float,
    ) -> None:
        """保存安全二创计划；已生成的成片不会被自动重置。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM video_jobs WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if row and row["status"] in {
                "queued",
                "generating",
                "rendering",
                "ready",
                "uncertain",
            }:
                return
            conn.execute(
                """
                INSERT INTO video_jobs (
                    source_id, plan_json, plan_model, quality, status, progress,
                    estimated_cost_cny, output_path, error, updated_at
                ) VALUES (?, ?, ?, 'preview', 'planned', 0, ?, '', '', ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    plan_json=excluded.plan_json, plan_model=excluded.plan_model,
                    estimated_cost_cny=excluded.estimated_cost_cny,
                    status='planned', progress=0, error='', updated_at=excluded.updated_at
                """,
                (
                    source_id,
                    json.dumps(plan, ensure_ascii=False),
                    model,
                    float(estimated_cost_cny),
                    self._now(),
                ),
            )

    def claim_video_job(self, source_id: str, quality: str, quote: dict) -> bool:
        """原子领取一条付费任务，防止并发点击重复提交。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status FROM video_jobs WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if not current or current["status"] not in {"planned", "failed"}:
                return False
            changed = conn.execute(
                """
                UPDATE video_jobs SET status='queued', progress=0, quality=?,
                    estimated_cost_cny=?, quote_id=?, quote_model=?,
                    generation_id=?, output_path='', error='', updated_at=?
                WHERE source_id=? AND status IN ('planned', 'failed')
                """,
                (
                    quality,
                    float(quote["total_cost_cny"]),
                    str(quote["quote_id"]),
                    str(quote["model"]),
                    str(quote["generation_id"]),
                    self._now(),
                    source_id,
                ),
            ).rowcount
        return changed == 1

    def list_incomplete_video_jobs(self) -> List[dict]:
        """列出重启后需要安全续跑的本机视频任务。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_id, quality FROM video_jobs
                WHERE status IN ('queued', 'generating', 'rendering')
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def update_video_job(
        self,
        source_id: str,
        *,
        status: str,
        progress: int,
        quality: Optional[str] = None,
        estimated_cost_cny: Optional[float] = None,
        output_path: str = "",
        error: str = "",
    ) -> None:
        """更新本机生成进度；状态只允许明确的有限集合。"""
        allowed = {
            "planned",
            "queued",
            "generating",
            "rendering",
            "ready",
            "failed",
            "uncertain",
        }
        if status not in allowed:
            raise PipelineError("未知视频任务状态")
        with self._connect() as conn:
            current = conn.execute(
                "SELECT * FROM video_jobs WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if not current:
                raise PipelineError("视频计划不存在")
            conn.execute(
                """
                UPDATE video_jobs SET status=?, progress=?, quality=?,
                    estimated_cost_cny=?, output_path=?, error=?, updated_at=?
                WHERE source_id=?
                """,
                (
                    status,
                    max(0, min(100, int(progress))),
                    quality or current["quality"],
                    (
                        float(estimated_cost_cny)
                        if estimated_cost_cny is not None
                        else current["estimated_cost_cny"]
                    ),
                    output_path or current["output_path"],
                    error[:500],
                    self._now(),
                    source_id,
                ),
            )

    def get_video_job(self, source_id: str) -> Optional[dict]:
        """读取一条视频计划与生成状态。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM video_jobs WHERE source_id=?",
                (source_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["plan"] = json.loads(result.pop("plan_json"))
        return result

    def mark_failed(self, source_id: str, message: str) -> None:
        """记录可重试错误。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE assets SET status='failed', error=?, updated_at=? WHERE source_id=?",
                (message[:500], self._now(), source_id),
            )

    def get_asset(self, source_id: str) -> Optional[dict]:
        """读取一条本机索引。"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM assets WHERE source_id=?", (source_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json")) if result.get("metadata_json") else None
        return result

    def list_assets(self) -> List[dict]:
        """列出本机索引和草稿数量。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT a.*, COUNT(d.platform) AS draft_count,
                    v.status AS video_status, v.progress AS video_progress,
                    v.quality AS video_quality,
                    v.estimated_cost_cny AS video_estimated_cost_cny,
                    v.output_path AS video_output_path,
                    v.error AS video_error, v.plan_json AS video_plan_json
                FROM assets a LEFT JOIN drafts d ON d.source_id=a.source_id
                LEFT JOIN video_jobs v ON v.source_id=a.source_id
                GROUP BY a.source_id ORDER BY a.updated_at DESC
                """
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json")) if item.get("metadata_json") else None
            raw_plan = item.pop("video_plan_json", None)
            item["video_plan"] = json.loads(raw_plan) if raw_plan else None
            result.append(item)
        return result

    def count_assets(self) -> int:
        """返回本机资产数。"""
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0])

    def list_drafts(self, source_id: str) -> List[dict]:
        """列出一条内容的九平台草稿。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM drafts WHERE source_id=? ORDER BY platform",
                (source_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["fields"] = json.loads(item.pop("fields_json"))
            result.append(item)
        return result

    def transition_draft(
        self,
        source_id: str,
        platform: str,
        target: str,
        *,
        expected_version: int,
    ) -> int:
        """按版本推进草稿状态；必填发布目标缺失时禁止 ready。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM drafts WHERE source_id=? AND platform=?",
                (source_id, platform),
            ).fetchone()
            if not row:
                raise PipelineError("草稿不存在")
            if int(row["version"]) != expected_version:
                raise PipelineError("版本冲突")
            fields = json.loads(row["fields_json"])
            if target == "ready":
                required = {"reddit": "subreddit", "pinterest": "board", "quora": "question"}.get(platform)
                if required and not fields.get(required):
                    raise PipelineError(f"{platform} 缺少 {required}")
            allowed = {
                "draft": {"pending_review"},
                "pending_review": {"approved", "rejected"},
                "approved": {"ready"},
                "ready": {"submitted"},
                "rejected": {"draft"},
            }
            if target not in allowed.get(row["status"], set()):
                raise PipelineError(f"不能从 {row['status']} 进入 {target}")
            version = expected_version + 1
            conn.execute(
                "UPDATE drafts SET status=?, version=?, updated_at=? WHERE source_id=? AND platform=?",
                (target, version, self._now(), source_id, platform),
            )
        return version

    def export_for_sync(self, source_id: str) -> dict:
        """只导出共享需要的字段，绝不导出本机路径和完整口播。"""
        asset = self.get_asset(source_id)
        if not asset:
            raise PipelineError("资产不存在")
        metadata = asset.get("metadata") or {}
        return {
            "source_id": asset["source_id"],
            "asset_type": "video",
            "title": asset["source_title"],
            "custom_title": metadata.get("title", ""),
            "author": asset["author"],
            "sha256": asset["sha256"],
            "size_bytes": asset["size_bytes"],
            "local_path": "",
            "rights": "external_source",
            "make": metadata.get("make", ""),
            "model": metadata.get("model", ""),
            "year_start": metadata.get("year_start"),
            "year_end": metadata.get("year_end"),
            "topic": metadata.get("topic", ""),
            "summary": metadata.get("summary", ""),
            "confidence": metadata.get("confidence", 0),
            "review_required": metadata.get("review_required", True),
            "evidence_scope": "transcript_only",
            "drafts": [
                {
                    "platform": row["platform"],
                    "fields": row["fields"],
                    "status": row["status"],
                    "version": row["version"],
                    "model": row["model"],
                }
                for row in self.list_drafts(source_id)
            ],
        }


class ContentPipeline:
    """扫描、AI 处理和本机落库的总调度器。"""

    def __init__(
        self,
        store: ContentIndexStore,
        service: AIContentService,
        video_planner=None,
    ):
        self.store = store
        self.service = service
        self.video_planner = video_planner

    def run(self, share_root: Path, inbox_root: Path) -> dict:
        """只处理新增或缺步骤素材；单条失败不阻塞其余素材。"""
        report = scan_sources(share_root, inbox_root)
        processed = 0
        planned = 0
        skipped = 0
        failed = 0
        for item in report.items:
            self.store.upsert_asset(item.asset_record())
            try:
                existing = self.store.get_asset(item.source_id) or {}
                draft_count = len(self.store.list_drafts(item.source_id))
                video_job = self.store.get_video_job(item.source_id)
                if existing.get("metadata") and draft_count == len(PLATFORMS):
                    if self.video_planner and not video_job:
                        from .content_video_pipeline import estimate_plan_cost

                        plan, plan_model = self.video_planner.create(
                            existing["metadata"],
                            item.transcript,
                        )
                        self.store.save_video_plan(
                            item.source_id,
                            plan,
                            plan_model,
                            estimated_cost_cny=estimate_plan_cost(plan, "preview"),
                        )
                        planned += 1
                    else:
                        skipped += 1
                    continue
                metadata, metadata_model = self.service.analyze(item)
                drafts, draft_model = self.service.make_drafts(metadata, item.transcript)
                self.store.save_metadata(item.source_id, metadata, metadata_model, confirmed=False)
                self.store.save_drafts(item.source_id, drafts, draft_model)
                if self.video_planner:
                    from .content_video_pipeline import estimate_plan_cost

                    plan, plan_model = self.video_planner.create(metadata, item.transcript)
                    self.store.save_video_plan(
                        item.source_id,
                        plan,
                        plan_model,
                        estimated_cost_cny=estimate_plan_cost(plan, "preview"),
                    )
                    planned += 1
                processed += 1
            except PipelineError as exc:
                failed += 1
                self.store.mark_failed(item.source_id, str(exc))
                log.warning("content pipeline failed source_id=%s err=%s", item.source_id, exc)
        return {
            **report.counts,
            "processed": processed,
            "planned": planned,
            "skipped": skipped,
            "failed": failed,
            "pending": report.pending,
        }


class AdsContentSyncClient:
    """把元数据和草稿同步到 Ads；不发送原片、路径或完整口播。"""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.transport = transport

    def sync(self, assets: List[dict]) -> dict:
        """登录同站账号并执行一次幂等同步。"""
        if not self.username or not self.password:
            raise PipelineError("Ads 同步账号尚未配置到系统凭证库")
        with httpx.Client(
            base_url=self.base_url,
            transport=self.transport,
            follow_redirects=False,
            timeout=30,
        ) as client:
            login = client.post(
                "/login",
                data={"username": self.username, "password": self.password},
            )
            if login.status_code != 302 or "session_token" not in client.cookies:
                raise PipelineError("Ads 登录失败，请检查共享账号凭证")
            response = client.post(
                "/api/content-index/sync",
                json={"team_id": "maxcool", "assets": assets},
            )
            if response.status_code == 409:
                raise PipelineError("共享索引版本冲突，请刷新后重试")
            if response.status_code != 200:
                raise PipelineError(f"Ads 同步失败：HTTP {response.status_code}")
            try:
                result = response.json()
            except json.JSONDecodeError as exc:
                raise PipelineError("Ads 同步返回格式错误") from exc
            return result


class AdsAIJSONClient:
    """通过公司共享 API 调用云端 AI，客户端不持有模型密钥。"""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client = httpx.Client(
            base_url=self.base_url,
            transport=transport,
            follow_redirects=False,
            timeout=90,
        )
        self.logged_in = False

    def _login(self) -> None:
        """按需登录一次并复用安全会话。"""
        if self.logged_in:
            return
        if not self.username or not self.password:
            raise PipelineError("公司 AI 账号尚未配置到系统凭证库")
        try:
            response = self.client.post(
                "/login",
                data={"username": self.username, "password": self.password},
            )
        except httpx.HTTPError as exc:
            raise PipelineError("公司 AI 网络连接失败") from exc
        if response.status_code != 302 or "session_token" not in self.client.cookies:
            raise PipelineError("公司 AI 登录失败")
        self.logged_in = True

    def call_json(self, prompt: str) -> Tuple[dict, str]:
        """发送口播提示词，返回结构化结果；不上传原视频。"""
        self._login()
        try:
            response = self.client.post(
                "/api/content-index/ai-json",
                json={"prompt": prompt},
            )
        except httpx.HTTPError as exc:
            raise PipelineError("公司 AI 网络连接失败") from exc
        if response.status_code != 200:
            try:
                detail = response.json().get("error") or ""
            except json.JSONDecodeError:
                detail = ""
            raise PipelineError(detail or f"公司 AI 处理失败：HTTP {response.status_code}")
        try:
            payload = response.json()
            result = payload["result"]
            provider = payload["provider"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise PipelineError("公司 AI 返回格式错误") from exc
        if not isinstance(result, dict):
            raise PipelineError("公司 AI 未返回 JSON 对象")
        return result, str(provider)

    def close(self) -> None:
        """释放网络连接。"""
        self.client.close()
