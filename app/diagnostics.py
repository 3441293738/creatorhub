"""诊断摘要、近期任务异常采集与脱敏诊断压缩包构建器。

用于帮助用户快速向开发者提报问题：
- 自动收集系统环境、核心依赖可用性、各平台通道模式、匿名化账号健康态与队列统计。
- 自动聚合近期失败/阻断的任务、风控信号与告警错误日志。
- 全链路剔除 Cookie、storage_state、API Key、代理账密、账号昵称/UID、私信/评论原文与本机用户名。
"""
from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
import json
import os
import platform
import re
import sys
import zipfile

from sqlalchemy import func
from sqlmodel import select

from app import runtime as app_runtime
from app.db import get_session
from app.logging_setup import (
    get_memory_handler,
    get_runtime_log_level,
    log_files_summary,
    read_log_tail,
    resolve_log_dir,
    sanitize_for_export,
    started_at_iso,
    uptime_seconds,
)
from app.models import (
    AccountActionTask,
    AccountOperationLog,
    BrowserRuntime,
    CommentTask,
    CommentWatch,
    DanmakuWatch,
    DouyinAccount,
    KeywordCollectionJob,
    MonitorTarget,
    ProxyPool,
    PublishTask,
    RiskEvent,
)
from app.runtime_tools import find_ffmpeg, find_node
from app.settings import get_setting


def detect_app_version() -> str:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    ver_file = root / "desktop-version.txt"
    if ver_file.is_file():
        try:
            text = ver_file.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    changelog = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
    if changelog.is_file():
        try:
            head = changelog.read_text(encoding="utf-8")[:12000]
            match = re.search(r"^##\s+\[?(v?\d+\.\d+\.\d+[^\]]*)\]?", head, re.MULTILINE)
            if match:
                return f"source ({match.group(1)})"
        except OSError:
            pass
    return "source"


def _configured_secrets() -> tuple[str, ...]:
    secrets_list: list[str] = []
    try:
        ai_key = get_setting("ai_api_key", "")
        if ai_key and len(ai_key) >= 6:
            secrets_list.append(ai_key)
    except Exception:
        pass
    return tuple(secrets_list)


def _group_counts(session, model, field_attr) -> dict[str, int]:
    rows = session.exec(select(field_attr, func.count()).group_by(field_attr)).all()
    return {str(k or "unknown"): int(v or 0) for k, v in rows}


def _safe_engine_snapshot() -> dict[str, Any]:
    eng = app_runtime.cfg.engine
    rc = app_runtime.cfg.risk_control
    return {
        "scan_interval_seconds": eng.scan_interval_seconds,
        "worker_pool_size": eng.worker_pool_size,
        "scan_concurrency": eng.scan_concurrency,
        "active_accounts": eng.active_accounts,
        "browser_backend": eng.browser_backend,
        "resident_browser_sessions": eng.resident_browser_sessions,
        "xhs_browser_mode": eng.xhs_browser_mode,
        "read_modes": {
            "douyin": eng.douyin_read_mode,
            "xhs": eng.xhs_read_mode,
            "rednote": eng.rednote_read_mode,
            "tiktok": eng.tiktok_read_mode,
            "bilibili": eng.bilibili_read_mode,
            "instagram": eng.instagram_read_mode,
            "twitter": eng.twitter_read_mode,
            "channels": eng.channels_read_mode,
        },
        "write_modes": {
            "douyin": eng.douyin_write_mode,
            "xhs_publish": eng.xhs_publish_mode,
            "xhs_comment": eng.xhs_comment_write_mode,
            "rednote_publish": eng.rednote_publish_mode,
            "bilibili": eng.bilibili_write_mode,
            "instagram": eng.instagram_write_mode,
            "twitter": eng.twitter_write_mode,
        },
        "quiet_hours": {
            "enabled": eng.quiet_hours_enabled,
            "include_reads": eng.quiet_hours_include_reads,
            "start": eng.active_hours_start,
            "end": eng.active_hours_end,
        },
        "native_write_gate": {
            "enabled": eng.native_write_gate_enabled,
            "require_system_chrome": eng.native_write_require_system_chrome,
            "require_verified_proxy": eng.native_write_require_verified_proxy,
        },
        "risk_control": {
            "enabled": rc.enabled,
            "mode": rc.mode,
            "network_group_concurrency": rc.network_group_concurrency,
        },
    }


def _runtime_dependencies() -> dict[str, Any]:
    patchright_ok = False
    try:
        from patchright._impl._driver import compute_driver_executable
        patchright_ok = all(Path(p).is_file() for p in compute_driver_executable())
    except Exception:
        patchright_ok = False

    channels_proxy_info: dict[str, Any] = {}
    try:
        st = app_runtime.channels_proxy.status()
        channels_proxy_info = {
            "running": bool(st.get("running")),
            "proxy_port": st.get("proxy_port"),
            "ca_installed": bool(st.get("ca_installed")),
            "connected_pages": int(st.get("connected_count") or 0),
        }
    except Exception:
        channels_proxy_info = {"running": False}

    channels_bridge_info: dict[str, Any] = {}
    try:
        bst = app_runtime.channels_bridge.status(include_processes=False)
        channels_bridge_info = {
            "running": bool(bst.get("running")),
            "native_connected": bool(bst.get("native_connected")),
            "cdp_connected": bool(bst.get("cdp_connected")),
        }
    except Exception:
        channels_bridge_info = {"running": False}

    return {
        "node_available": bool(find_node()),
        "ffmpeg_available": bool(find_ffmpeg()),
        "patchright_driver_available": patchright_ok,
        "engine_running": bool(getattr(app_runtime.engine, "_running", False)),
        "browser_manager_ready": app_runtime.browser is not None,
        "open_headed_browsers": len(app_runtime.open_browsers),
        "channels_proxy": channels_proxy_info,
        "channels_wmpf_bridge": channels_bridge_info,
    }


def build_diagnostic_summary() -> dict[str, Any]:
    """构建脱敏的系统、配置、账号匿名健康态与任务统计快照。"""
    secrets = _configured_secrets()
    now_utc = datetime.utcnow()

    accounts_anon: list[dict[str, Any]] = []
    proxy_stats: dict[str, int] = {}
    runtime_count = 0
    task_stats: dict[str, Any] = {}

    try:
        with get_session() as session:
            for acc in session.exec(select(DouyinAccount).order_by(DouyinAccount.id)).all():
                paused = bool(acc.write_paused_until and acc.write_paused_until > now_utc)
                accounts_anon.append({
                    "account_id": acc.id,
                    "platform": acc.platform,
                    "status": acc.status,
                    "role": acc.account_role or "hybrid",
                    "identity_mode": acc.identity_mode,
                    "browser_backend": acc.browser_backend,
                    "has_cookie_or_state": bool(acc.storage_state or acc.creator_storage_state or acc.cookie),
                    "has_proxy": bool(acc.proxy),
                    "proxy_status": acc.proxy_status,
                    "write_paused": paused,
                    "write_pause_reason": sanitize_for_export(acc.write_pause_reason, extra_secrets=secrets)[:200]
                    if acc.write_pause_reason
                    else "",
                })
            proxy_stats = _group_counts(session, ProxyPool, ProxyPool.status)
            runtime_count = int(session.exec(select(func.count()).select_from(BrowserRuntime)).one() or 0)
            task_stats = {
                "monitors_total": int(session.exec(select(func.count()).select_from(MonitorTarget)).one() or 0),
                "monitors_enabled": int(
                    session.exec(
                        select(func.count()).select_from(MonitorTarget).where(MonitorTarget.enabled == True)  # noqa: E712
                    ).one()
                    or 0
                ),
                "comment_watches_enabled": int(
                    session.exec(
                        select(func.count()).select_from(CommentWatch).where(CommentWatch.enabled == True)  # noqa: E712
                    ).one()
                    or 0
                ),
                "danmaku_watches_enabled": int(
                    session.exec(
                        select(func.count()).select_from(DanmakuWatch).where(DanmakuWatch.enabled == True)  # noqa: E712
                    ).one()
                    or 0
                ),
                "collections_by_status": _group_counts(session, KeywordCollectionJob, KeywordCollectionJob.status),
                "publish_by_status": _group_counts(session, PublishTask, PublishTask.status),
                "comment_tasks_by_status": _group_counts(session, CommentTask, CommentTask.status),
                "action_tasks_by_status": _group_counts(session, AccountActionTask, AccountActionTask.status),
            }
    except Exception as exc:
        task_stats = {"db_query_error": sanitize_for_export(repr(exc), extra_secrets=secrets)}

    logs_meta = log_files_summary(app_runtime.cfg)
    logs_meta["log_dir_masked"] = sanitize_for_export(logs_meta.get("log_dir", ""), extra_secrets=secrets)
    logs_meta.pop("log_dir", None)

    return {
        "schema": 1,
        "generated_at": now_utc.isoformat(timespec="seconds") + "Z",
        "app_version": detect_app_version(),
        "run_mode": "desktop" if os.environ.get("CREATORHUB_DESKTOP") == "1" or os.environ.get("CREATORHUB_DESKTOP_HOME") else "cli",
        "environment": {
            "os": platform.system(),
            "os_release": platform.release(),
            "os_version": platform.version(),
            "arch": platform.machine(),
            "python": platform.python_version(),
            "frozen": bool(getattr(sys, "frozen", False)),
            "started_at": started_at_iso(),
            "uptime_seconds": uptime_seconds(),
        },
        "dependencies": {
            **_runtime_dependencies(),
            "registered_browser_runtimes": runtime_count,
        },
        "logging": logs_meta,
        "engine_config": _safe_engine_snapshot(),
        "proxy_pool_counts": proxy_stats,
        "accounts_health": accounts_anon,
        "task_queues": task_stats,
    }


def collect_recent_issues(*, limit: int = 30) -> dict[str, Any]:
    """从数据库提取近期失败任务、风控触发与异常操作记录（不含用户昵称或作品/评论正文）。"""
    secrets = _configured_secrets()
    cap = max(5, min(100, int(limit or 30)))

    def clean(val: Any, max_len: int = 400) -> str:
        return sanitize_for_export(val, extra_secrets=secrets)[:max_len]

    failed_ops: list[dict[str, Any]] = []
    risk_events: list[dict[str, Any]] = []
    failed_tasks: list[dict[str, Any]] = []

    try:
        with get_session() as session:
            op_rows = session.exec(
                select(AccountOperationLog)
                .where(AccountOperationLog.status.in_(["failed", "error", "blocked", "uncertain", "partial"]))
                .order_by(AccountOperationLog.id.desc())
                .limit(cap)
            ).all()
            for row in op_rows:
                failed_ops.append({
                    "id": row.id,
                    "created_at": row.created_at.isoformat(timespec="seconds") + "Z" if row.created_at else None,
                    "platform": row.platform,
                    "account_id": row.account_id,
                    "task_type": row.task_type,
                    "task_id": row.task_id,
                    "action": row.action,
                    "channel": row.channel,
                    "status": row.status,
                    "risk_signal": clean(row.risk_signal, 120),
                    "duration_ms": row.duration_ms,
                    "detail": clean(row.detail, 500),
                })

            r_rows = session.exec(
                select(RiskEvent)
                .where(RiskEvent.outcome != "success")
                .order_by(RiskEvent.id.desc())
                .limit(cap)
            ).all()
            for r in r_rows:
                risk_events.append({
                    "id": r.id,
                    "occurred_at": r.occurred_at.isoformat(timespec="seconds") + "Z" if r.occurred_at else None,
                    "account_id": r.account_id,
                    "operation_kind": r.operation_kind,
                    "outcome": r.outcome,
                    "signal": clean(r.signal, 120),
                    "detail": clean(r.detail, 300),
                })

            for pub in session.exec(
                select(PublishTask)
                .where(PublishTask.status.in_(["failed", "uncertain"]))
                .order_by(PublishTask.id.desc())
                .limit(15)
            ).all():
                failed_tasks.append({
                    "kind": "publish",
                    "id": pub.id,
                    "platform": pub.platform,
                    "account_id": pub.account_id,
                    "media_type": pub.media_type,
                    "status": pub.status,
                    "blocked_signal": clean(pub.blocked_signal, 120),
                    "error": clean(pub.error or pub.blocked_reason, 500),
                    "created_at": pub.created_at.isoformat(timespec="seconds") + "Z" if pub.created_at else None,
                })

            for col in session.exec(
                select(KeywordCollectionJob)
                .where(KeywordCollectionJob.status.in_(["failed", "partial"]))
                .order_by(KeywordCollectionJob.id.desc())
                .limit(15)
            ).all():
                failed_tasks.append({
                    "kind": "collection",
                    "id": col.id,
                    "platform": col.platform,
                    "account_id": col.account_id,
                    "source_type": col.source_type,
                    "status": col.status,
                    "content_count": col.content_count,
                    "error_count": col.error_count,
                    "blocked_signal": clean(col.blocked_signal, 120),
                    "error": clean(col.error or col.blocked_reason, 500),
                    "created_at": col.created_at.isoformat(timespec="seconds") + "Z" if col.created_at else None,
                })

            for cmt in session.exec(
                select(CommentTask)
                .where(CommentTask.status.in_(["failed", "uncertain"]))
                .order_by(CommentTask.id.desc())
                .limit(15)
            ).all():
                failed_tasks.append({
                    "kind": "comment_task",
                    "id": cmt.id,
                    "platform": cmt.platform,
                    "account_id": cmt.account_id,
                    "method": cmt.method,
                    "status": cmt.status,
                    "blocked_signal": clean(cmt.blocked_signal, 120),
                    "error": clean(cmt.error or cmt.blocked_reason, 400),
                    "created_at": cmt.created_at.isoformat(timespec="seconds") + "Z" if cmt.created_at else None,
                })

            for mon in session.exec(
                select(MonitorTarget)
                .where(MonitorTarget.last_error != "")
                .order_by(MonitorTarget.id.desc())
                .limit(15)
            ).all():
                failed_tasks.append({
                    "kind": "monitor",
                    "id": mon.id,
                    "platform": mon.platform,
                    "account_id": mon.account_id,
                    "target_kind": mon.target_kind,
                    "enabled": mon.enabled,
                    "last_scan_at": mon.last_scan_at.isoformat(timespec="seconds") + "Z" if mon.last_scan_at else None,
                    "error": clean(mon.last_error, 400),
                })
    except Exception as exc:
        return {"query_error": clean(repr(exc))}

    return {
        "failed_operations": failed_ops,
        "risk_events": risk_events,
        "failed_tasks": failed_tasks,
    }


def build_clipboard_report(note: str = "") -> str:
    """生成一段精简易读的 Markdown 排查摘要，方便用户直接粘贴到聊天框或 Issue。"""
    secrets = _configured_secrets()
    summary = build_diagnostic_summary()
    issues = collect_recent_issues(limit=10)
    env = summary["environment"]
    deps = summary["dependencies"]

    accs = summary.get("accounts_health") or []
    invalid_accs = [a for a in accs if a.get("status") != "active"]
    paused_accs = [a for a in accs if a.get("write_paused")]

    lines: list[str] = [
        "### CreatorHub 问题排查摘要",
        f"- **生成时间**: {summary['generated_at']} (运行 {env['uptime_seconds']}s)",
        f"- **版本/模式**: `{summary['app_version']}` · `{summary['run_mode']}` · 日志级别 `{get_runtime_log_level().upper()}`",
        f"- **系统环境**: {env['os']} {env['os_release']} ({env['arch']}) · Python {env['python']}",
        f"- **核心组件**: Patchright={'OK' if deps['patchright_driver_available'] else '缺失'} · "
        f"Node={'OK' if deps['node_available'] else '缺失'} · "
        f"FFmpeg={'OK' if deps['ffmpeg_available'] else '缺失'} · "
        f"引擎={'运行中' if deps['engine_running'] else '未启动'}",
        f"- **账号概览**: 共 {len(accs)} 个账号（登录失效 {len(invalid_accs)} 个，风控暂停写入 {len(paused_accs)} 个）",
    ]

    clean_note = sanitize_for_export(note.strip(), extra_secrets=secrets)[:1000] if note and note.strip() else ""
    if clean_note:
        lines.append(f"- **用户问题描述**: {clean_note}")

    failed_tasks = (issues.get("failed_tasks") or [])[:6]
    if failed_tasks:
        lines.append("\n**近期失败/异常任务 (最多 6 条)**:")
        for item in failed_tasks:
            lines.append(
                f"- `[{item['kind']}#{item['id']}]` {item.get('platform', '-')} "
                f"(账号#{item.get('account_id') or '-'}, status={item.get('status', '-')}) -> {item.get('error', '')}"
            )

    warn_entries = get_memory_handler().query(level="warn", limit=12)
    if warn_entries:
        lines.append("\n**近期告警与错误日志 (最新 12 条)**:")
        lines.append("```text")
        for entry in warn_entries:
            msg = sanitize_for_export(entry["message"], extra_secrets=secrets)
            lines.append(f"{entry['timestamp']} | {entry['level']:5s} | {entry['logger']} | {msg[:240]}")
            if entry.get("exception"):
                exc_tail = sanitize_for_export(entry["exception"], extra_secrets=secrets).splitlines()[-3:]
                for el in exc_tail:
                    lines.append(f"  {el[:240]}")
        lines.append("```")
    else:
        log_dir = resolve_log_dir(app_runtime.cfg)
        err_tail = read_log_tail(log_dir / "error.log", max_lines=12, max_bytes=16384, for_export=True)
        if err_tail:
            lines.append("\n**error.log 尾部记录**:")
            lines.append("```text")
            lines.append(err_tail)
            lines.append("```")
        else:
            lines.append("\n- 当前暂无 WARNING / ERROR 级别异常日志。")

    return "\n".join(lines) + "\n"


def build_diagnostic_zip(note: str = "") -> bytes:
    """打包脱敏后的完整排障压缩包 (.zip)，体积可控（通常 < 300 KB）。"""
    secrets = _configured_secrets()
    summary = build_diagnostic_summary()
    issues = collect_recent_issues(limit=40)
    report_md = build_clipboard_report(note=note)
    log_dir = resolve_log_dir(app_runtime.cfg)

    readme_text = (
        "CreatorHub 脱敏排障诊断包\n"
        "========================\n"
        "此压缩包专供向开发者反馈问题定位原因使用，导出时已自动剥离以下隐私数据：\n"
        "- 所有账号的 Cookie、storage_state、登录令牌、签名参数\n"
        "- 大模型 API Key、Webhook 密钥、代理服务器账号密码\n"
        "- 账号昵称、平台 UID/sec_uid、评论正文、私信聊天内容\n"
        "- 本机系统用户名目录与公网出口 IP 后两段\n\n"
        "文件清单：\n"
        "- issue-report.md     可读问题摘要（含用户填写的问题说明）\n"
        "- summary.json        环境、依赖、通道配置与匿名化账号/队列状态\n"
        "- recent-issues.json  近期失败任务、操作错误与风控信号记录\n"
        "- error.log           近期告警与错误堆栈日志（尾部最多 600 行）\n"
        "- app.log             近期精简运行日志（尾部最多 1200 行）\n"
    )

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", readme_text)
        zf.writestr("issue-report.md", report_md)
        zf.writestr("summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        zf.writestr("recent-issues.json", json.dumps(issues, ensure_ascii=False, indent=2) + "\n")

        err_tail = read_log_tail(log_dir / "error.log", max_lines=600, max_bytes=256 * 1024, for_export=True)
        zf.writestr("error.log", (err_tail + "\n") if err_tail else "# 暂无告警或错误日志\n")

        app_tail = read_log_tail(log_dir / "app.log", max_lines=1200, max_bytes=512 * 1024, for_export=True)
        zf.writestr("app.log", (app_tail + "\n") if app_tail else "# 暂无常规运行日志\n")

        # 若为桌面端运行，附带最近一份子进程标准流兜底日志尾部（排查原生浏览器/C++ 级错误）
        stdio_candidates = sorted(
            [
                p for p in log_dir.glob("service-stdio*.log") if p.is_file()
            ] + [
                p for p in log_dir.glob("desktop-*.log") if p.is_file()
            ],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if stdio_candidates:
            stdio_tail = read_log_tail(
                stdio_candidates[0], max_lines=300, max_bytes=128 * 1024, for_export=True
            )
            if stdio_tail:
                zf.writestr("service-stdio-tail.log", stdio_tail + "\n")

        if note and note.strip():
            zf.writestr(
                "user-note.txt",
                sanitize_for_export(note.strip(), extra_secrets=secrets)[:4000] + "\n",
            )

    return buf.getvalue()
