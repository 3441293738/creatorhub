"""日志查看、运行期调试级别切换、打开日志目录、一键复制摘要与脱敏诊断包导出接口。"""
from __future__ import annotations

from datetime import datetime
import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import runtime as app_runtime
from app.diagnostics import (
    build_clipboard_report,
    build_diagnostic_summary,
    build_diagnostic_zip,
)
from app.logging_setup import (
    clear_log_files,
    get_memory_handler,
    log_files_summary,
    read_log_tail,
    resolve_log_dir,
    set_runtime_log_level,
)
from app.services.contents import _reveal_in_file_manager

router = APIRouter()

_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s*\|\s*([A-Z]+)\s*\|\s*([^|]+?)\s*\|\s*(?:\[req=([^\]]+)\]\s*)?(.*)$"
)


class LogLevelIn(BaseModel):
    level: str = Field(..., description="info | debug | warning")


class IssueNoteIn(BaseModel):
    note: str = Field(default="", max_length=2000)


def _parse_file_tail_entries(
    raw_text: str,
    *,
    level_filter: str = "all",
    logger_filter: str = "",
    keyword: str = "",
    limit: int = 150,
) -> list[dict]:
    if not raw_text:
        return []
    entries: list[dict] = []
    current: dict | None = None
    for line in raw_text.splitlines():
        m = _LOG_LINE_RE.match(line)
        if m:
            if current is not None:
                entries.append(current)
            ts, lvl_raw, lg_raw, req_id, msg = m.groups()
            lvl_norm = "WARNING" if lvl_raw.startswith("WARN") else ("CRITICAL" if lvl_raw.startswith("CRIT") else lvl_raw)
            current = {
                "timestamp": ts,
                "level": lvl_norm,
                "logger": lg_raw.strip(),
                "req_id": (req_id or "").strip(),
                "message": msg.strip(),
                "exception": "",
            }
        elif current is not None:
            current["exception"] = (current["exception"] + "\n" + line) if current["exception"] else line
        else:
            current = {
                "timestamp": "",
                "level": "INFO",
                "logger": "file",
                "req_id": "",
                "message": line,
                "exception": "",
            }
    if current is not None:
        entries.append(current)

    lvl = str(level_filter or "all").strip().lower()
    lf = str(logger_filter or "").strip().lower()
    kw = str(keyword or "").strip().lower()
    matched: list[dict] = []
    for item in reversed(entries):
        il = item["level"]
        if lvl == "error" and il not in {"ERROR", "CRITICAL"}:
            continue
        if lvl in {"warn", "warning"} and il not in {"WARNING", "ERROR", "CRITICAL"}:
            continue
        if lf and lf not in item["logger"].lower():
            continue
        if kw and kw not in item["message"].lower() and kw not in item["exception"].lower() and kw not in item["logger"].lower():
            continue
        matched.append(item)
        if len(matched) >= limit:
            break
    return matched


@router.get("/api/diagnostics/logs")
async def get_diagnostic_logs(
    level: str = Query("warn", description="all | warn | error"),
    source: str = Query("memory", description="memory | error.log | app.log"),
    logger: str = Query("", max_length=60),
    q: str = Query("", max_length=120),
    limit: int = Query(150, ge=10, le=500),
):
    info = log_files_summary(app_runtime.cfg)
    log_dir = resolve_log_dir(app_runtime.cfg)
    src = str(source or "memory").strip().lower()
    if src in {"error.log", "app.log"}:
        raw = read_log_tail(log_dir / src, max_lines=600, max_bytes=256 * 1024, for_export=False)
        entries = _parse_file_tail_entries(
            raw, level_filter=level, logger_filter=logger, keyword=q, limit=limit
        )
    else:
        entries = get_memory_handler().query(
            level=level, logger_filter=logger, keyword=q, limit=limit
        )
    return {
        **info,
        "source": src if src in {"error.log", "app.log"} else "memory",
        "entries": entries,
    }


@router.get("/api/diagnostics/summary")
async def get_diagnostic_summary():
    return build_diagnostic_summary()


@router.put("/api/diagnostics/log-level")
async def put_diagnostic_log_level(body: LogLevelIn):
    try:
        applied = set_runtime_log_level(body.level)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {
        "ok": True,
        "level": applied,
        "note": (
            "已临时开启详细调试日志(DEBUG)，复现问题导出后建议切回精简模式(INFO)；重启后会自动恢复默认级别。"
            if applied == "debug"
            else f"已切换为 {applied.upper()} 级别"
        ),
    }


@router.post("/api/diagnostics/clear-logs")
async def post_clear_logs():
    return clear_log_files(app_runtime.cfg)


@router.post("/api/diagnostics/open-dir")
async def post_open_log_dir():
    log_dir = resolve_log_dir(app_runtime.cfg)
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        _reveal_in_file_manager(log_dir)
    except Exception as exc:
        raise HTTPException(500, f"无法打开日志目录: {exc}") from None
    return {"ok": True, "log_dir": str(log_dir)}


@router.post("/api/diagnostics/report-text")
async def post_diagnostic_report_text(body: IssueNoteIn):
    text = build_clipboard_report(note=body.note)
    return {"ok": True, "text": text}


@router.get("/api/diagnostics/export.zip")
async def export_diagnostic_bundle(note: str = Query("", max_length=2000)):
    archive = build_diagnostic_zip(note=note)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"creatorhub-diagnostics-{stamp}.zip"
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
