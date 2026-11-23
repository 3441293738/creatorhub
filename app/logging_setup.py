"""统一日志初始化、敏感信息脱敏、按体积滚动归档、内存环形缓冲与精简请求中间件。

设计原则：
1. 导入零副作用：模块导入阶段不打开任何日志文件，确保打包体检与静态导入安全。
2. 精简高信噪比：彻底屏蔽前端高频 GET 轮询与静态资源刷屏，仅记录关键生命周期、用户写操作、慢请求与告警/异常。
3. 默认安全脱敏：落盘与内存缓冲前自动抹除 Cookie、Authorization、API Key、代理账密与签名令牌；导出时进一步隐藏本机用户名与公网出口 IP 末段。
4. 磁盘占用可控：拆分为 app.log(常规排障) 与 error.log(仅告警/错误栈)，RotatingFileHandler 自动限容并清理陈旧会话日志。
"""
from __future__ import annotations

from collections import deque
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import ipaddress
import logging
import os
import re
import sys
import threading
import time
import traceback
import uuid

if TYPE_CHECKING:
    from app.config import Config

request_id_var: ContextVar[str] = ContextVar("creatorhub_req_id", default="-")
_STARTED_AT_MONO: float = time.monotonic()
_STARTED_AT_UTC: datetime = datetime.utcnow()

_MANAGED_ATTR = "_creatorhub_managed"
_LOCK = threading.RLock()
_ACTIVE_LOG_DIR: Path | None = None
_CURRENT_LEVEL_NAME: str = "info"
_MEMORY_HANDLER: "MemoryLogHandler | None" = None
_FILE_HANDLERS: list[RotatingFileHandler] = []

_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}

# 敏感 URL 查询参数（保留 platform/account_id/page/task_id 等排障参数）
_SENSITIVE_QUERY_KEYS = re.compile(
    r"^(?:cookie|cookies|token|access_token|refresh_token|xsec_token|mstoken|"
    r"a_bogus|x-bogus|x_bogus|x-gnarly|x_gnarly|x-dynosaur|x_dynosaur|"
    r"verifyfp|fp|sessionid|sessionid_ss|sid_guard|sid_tt|uid_tt|ttwid|"
    r"sessdata|bili_jct|dedeuserid__ckmd5|web_session|auth_token|ct0|"
    r"api_key|apikey|key|secret|password|passwd|pwd|signature|sig|ticket)$",
    re.IGNORECASE,
)

_HEADER_SECRET_RE = re.compile(
    r"(?im)\b(cookie|set-cookie|authorization|proxy-authorization|x-desktop-token)"
    r"([\"']?\s*[:=]\s*)(?!\[REDACTED\])(?:[^\r\n,;\"']+|\"[^\"]*\"|'[^']*')"
)
_KV_SECRET_RE = re.compile(
    r"(?i)\b(storage_state|creator_storage_state|ai_api_key|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|xsec[_-]?token|auth[_-]?token|token|"
    r"sessionid|sessdata|bili_jct|web_session|cookie|password|passwd|secret)"
    r"([\"']?\s*[:=]\s*)(?!\[REDACTED\])(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)
_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_URL_RE = re.compile(r"(?:https?|socks5h?|socks4a?|wss?)://[^\s<>\"')\]}]+", re.IGNORECASE)
_WIN_USER_PATH_RE = re.compile(r"([A-Za-z]:[\\/]+Users[\\/]+)([^\\/\s\"':;]+)([\\/])", re.IGNORECASE)
_UNIX_USER_PATH_RE = re.compile(r"(/(?:Users|home)/)([^/\s\"':;]+)(/)")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# 长连接流式路径不计入慢请求告警
_STREAMING_PATH_HINTS = ("/stream", "/events", "/api/agent/runs/")


def get_request_id() -> str:
    return request_id_var.get("-")


def uptime_seconds() -> int:
    return max(0, int(time.monotonic() - _STARTED_AT_MONO))


def started_at_iso() -> str:
    return _STARTED_AT_UTC.isoformat() + "Z"


def _clean_single_url(raw_url: str) -> str:
    trailing = ""
    while raw_url and raw_url[-1] in ".,;:!?)":
        trailing = raw_url[-1] + trailing
        raw_url = raw_url[:-1]
    try:
        parts = urlsplit(raw_url)
        hostname = parts.hostname or ""
        netloc_host = f"[{hostname}]" if ":" in hostname else hostname
        if parts.port:
            netloc_host = f"{netloc_host}:{parts.port}"
        if parts.username or parts.password:
            netloc = f"***:***@{netloc_host}"
        else:
            netloc = netloc_host
        query = parts.query
        if query:
            pairs = parse_qsl(query, keep_blank_values=True)
            cleaned_pairs = [
                (k, "[REDACTED]" if _SENSITIVE_QUERY_KEYS.match(k) else v)
                for k, v in pairs
            ]
            query = urlencode(cleaned_pairs, doseq=True)
            # urlencode encodes [REDACTED] brackets; restore readable marker
            query = query.replace("%5BREDACTED%5D", "[REDACTED]")
        return urlunsplit((parts.scheme, netloc, parts.path, query, "")) + trailing
    except Exception:
        return "[URL_REDACTED]" + trailing


def _mask_public_ipv4(match: re.Match[str]) -> str:
    candidate = match.group(0)
    try:
        ip = ipaddress.IPv4Address(candidate)
        if ip.is_loopback or ip.is_unspecified or ip.is_private or ip.is_link_local:
            return candidate
        octets = candidate.split(".")
        return f"{octets[0]}.{octets[1]}.*.*"
    except ValueError:
        return candidate


def sanitize_log_text(
    text: Any,
    *,
    extra_secrets: tuple[str, ...] = (),
    mask_paths: bool = False,
) -> str:
    """对日志文本执行快速确定性脱敏。"""
    if text is None:
        return ""
    value = text if isinstance(text, str) else str(text)
    if not value:
        return ""
    for secret in extra_secrets:
        if secret and len(secret) >= 6:
            value = value.replace(secret, "[REDACTED]")
    value = _SK_TOKEN_RE.sub("sk-[REDACTED]", value)
    value = _URL_RE.sub(lambda m: _clean_single_url(m.group(0)), value)
    value = _HEADER_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value)
    value = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value)
    if mask_paths:
        value = _WIN_USER_PATH_RE.sub(r"\1<USER>\3", value)
        value = _UNIX_USER_PATH_RE.sub(r"\1<USER>\3", value)
        value = _IPV4_RE.sub(_mask_public_ipv4, value)
    return value


def sanitize_for_export(text: Any, *, extra_secrets: tuple[str, ...] = ()) -> str:
    """用于导出诊断包或复制反馈摘要的深度脱敏（含本机用户名与公网 IP 掩码）。"""
    return sanitize_log_text(text, extra_secrets=extra_secrets, mask_paths=True)


def short_logger_name(name: str) -> str:
    if not name or name == "root":
        return "app"
    for prefix in ("creatorhub.", "app.platforms.", "app."):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if name in {"uvicorn.error", "uvicorn"}:
        return "server"
    if name.endswith(".publish"):
        plat = name.rsplit(".", 1)[0]
        return f"publish.{plat}"
    return name[:22]


class BurstDedupFilter(logging.Filter):
    """折叠短时间内完全相同的重复日志（如断网重试），避免刷屏冲掉关键上下文。"""

    def __init__(self, window_seconds: float = 60.0, max_repeats: int = 2) -> None:
        super().__init__()
        self.window_seconds = window_seconds
        self.max_repeats = max_repeats
        self._lock = threading.Lock()
        self._state: dict[tuple[int, str, str], list[float | int]] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        # 含异常栈的 ERROR 日志绝不折叠，保证每次崩溃栈完整可见
        if record.exc_info or record.levelno >= logging.ERROR:
            return True
        try:
            msg = record.getMessage()
        except Exception:
            return True
        key = (record.levelno, record.name, msg[:240])
        now = time.monotonic()
        with self._lock:
            if len(self._state) > 512:
                cutoff = now - self.window_seconds
                self._state = {k: v for k, v in self._state.items() if v[0] >= cutoff}
            entry = self._state.get(key)
            if entry is None or (now - float(entry[0])) > self.window_seconds:
                suppressed = int(entry[2]) if entry is not None else 0
                self._state[key] = [now, 1, 0]
                if suppressed > 0:
                    setattr(record, "_suppressed_repeats", suppressed)
                return True
            entry[1] = int(entry[1]) + 1
            if int(entry[1]) <= self.max_repeats:
                return True
            entry[2] = int(entry[2]) + 1
            return False


class CompactRedactingFormatter(logging.Formatter):
    """单行紧凑、带毫秒本地时间、自动关联 req_id 并脱敏的格式化器。"""

    MAX_MESSAGE_CHARS = 2000
    MAX_TRACEBACK_CHARS = 8000

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created)
        return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{int(record.msecs):03d}"

    _SHORT_LEVEL = {
        "WARNING": "WARN",
        "CRITICAL": "CRIT",
    }

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record)
        level = self._SHORT_LEVEL.get(record.levelname, record.levelname[:5]).ljust(5)
        logger_label = short_logger_name(record.name).ljust(16)
        try:
            raw_msg = record.getMessage()
        except Exception:
            raw_msg = str(record.msg)
        suppressed = getattr(record, "_suppressed_repeats", 0)
        if suppressed:
            raw_msg = f"{raw_msg} (此前相似日志已折叠 {suppressed} 条)"
        clean_msg = sanitize_log_text(raw_msg).replace("\r\n", "\n").strip()
        if len(clean_msg) > self.MAX_MESSAGE_CHARS:
            clean_msg = clean_msg[: self.MAX_MESSAGE_CHARS] + " …[truncated]"
        req_id = getattr(record, "req_id", None) or get_request_id()
        prefix = f"[req={req_id}] " if req_id and req_id != "-" else ""
        line = f"{ts} | {level} | {logger_label} | {prefix}{clean_msg}"

        exc_text = ""
        if record.exc_info:
            exc_text = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        elif record.exc_text:
            exc_text = str(record.exc_text).rstrip()
        if exc_text:
            exc_text = sanitize_log_text(exc_text)
            if len(exc_text) > self.MAX_TRACEBACK_CHARS:
                exc_text = exc_text[: self.MAX_TRACEBACK_CHARS] + "\n…[traceback truncated]"
            line = f"{line}\n{exc_text}"
        return line


class MemoryLogHandler(logging.Handler):
    """线程安全的内存环形日志缓冲，供设置页实时查看与快速导出。"""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__(level=logging.DEBUG)
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._seq = 0
        self._buf_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            raw_msg = record.getMessage()
        except Exception:
            raw_msg = str(record.msg)
        suppressed = getattr(record, "_suppressed_repeats", 0)
        if suppressed:
            raw_msg = f"{raw_msg} (此前相似日志已折叠 {suppressed} 条)"
        clean_msg = sanitize_log_text(raw_msg).strip()
        if len(clean_msg) > 2000:
            clean_msg = clean_msg[:2000] + " …[truncated]"
        exc_text = ""
        if record.exc_info:
            exc_text = sanitize_log_text(
                "".join(traceback.format_exception(*record.exc_info)).rstrip()
            )
            if len(exc_text) > 6000:
                exc_text = exc_text[:6000] + "\n…[traceback truncated]"
        req_id = getattr(record, "req_id", None) or get_request_id()
        item = {
            "timestamp": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S.")
            + f"{int(record.msecs):03d}",
            "epoch_ms": int(record.created * 1000),
            "level": record.levelname,
            "logger": short_logger_name(record.name),
            "req_id": "" if req_id == "-" else str(req_id),
            "message": clean_msg,
            "exception": exc_text,
        }
        with self._buf_lock:
            self._seq += 1
            item["seq"] = self._seq
            self._records.append(item)

    def query(
        self,
        *,
        level: str = "all",
        logger_filter: str = "",
        keyword: str = "",
        limit: int = 150,
    ) -> list[dict[str, Any]]:
        lvl = str(level or "all").strip().lower()
        min_level = (
            logging.ERROR
            if lvl == "error"
            else logging.WARNING
            if lvl in {"warn", "warning"}
            else logging.DEBUG
        )
        lf = str(logger_filter or "").strip().lower()
        kw = str(keyword or "").strip().lower()
        cap = max(1, min(500, int(limit or 150)))
        with self._buf_lock:
            snapshot = list(self._records)
        matched: list[dict[str, Any]] = []
        for row in reversed(snapshot):
            row_lvl = _LEVEL_MAP.get(row["level"].lower(), logging.INFO)
            if row_lvl < min_level:
                continue
            if lf and lf not in row["logger"].lower():
                continue
            if kw and kw not in row["message"].lower() and kw not in row["exception"].lower() and kw not in row["logger"].lower():
                continue
            matched.append(row)
            if len(matched) >= cap:
                break
        return matched

    def counts(self) -> dict[str, int]:
        with self._buf_lock:
            snapshot = list(self._records)
        warnings = sum(1 for r in snapshot if r["level"] == "WARNING")
        errors = sum(1 for r in snapshot if r["level"] in {"ERROR", "CRITICAL"})
        return {"total": len(snapshot), "warnings": warnings, "errors": errors}

    def clear(self) -> None:
        with self._buf_lock:
            self._records.clear()


def _ensure_utf8_stdio() -> None:
    seen: set[int] = set()
    for stream in (sys.stdout, sys.stderr):
        if stream is None or id(stream) in seen:
            continue
        seen.add(id(stream))
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass


def resolve_log_dir(cfg: "Config | None" = None) -> Path:
    if _ACTIVE_LOG_DIR is not None:
        return _ACTIVE_LOG_DIR
    env_dir = os.environ.get("CREATORHUB_LOG_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    desktop_home = os.environ.get("CREATORHUB_DESKTOP_HOME", "").strip()
    if desktop_home:
        return (Path(desktop_home).expanduser().resolve() / "logs")
    raw_dir = getattr(getattr(cfg, "logging", None), "dir", "") or "./logs"
    return Path(raw_dir).expanduser().resolve()


def _prune_housekeeping_files(log_dir: Path, cfg: "Config | None" = None) -> None:
    """清理历史无限累积的旧会话日志与调试快照，避免长期运行占满用户磁盘。"""
    for pattern, keep in (("desktop-*.log", 5), ("process-*.log", 5)):
        try:
            files = sorted(
                (p for p in log_dir.glob(pattern) if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for stale in files[keep:]:
                try:
                    stale.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    # 发布失败落盘的调试截图/HTML 保留最新 20 个文件即可
    try:
        db_parent = Path(getattr(cfg, "db_path", "./data/creatorhub.db")).resolve().parent
        debug_dir = db_parent / "debug"
        if debug_dir.is_dir():
            snaps = sorted(
                (p for p in debug_dir.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for stale in snaps[20:]:
                try:
                    stale.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _quiet_third_party_loggers() -> None:
    # 彻底关闭 uvicorn 默认访问日志(高频轮询噪声源头)，改由 RequestLoggingMiddleware 精简记录
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True

    # 让 uvicorn.error / fastapi 走根 logger 统一格式与落盘
    for name in ("uvicorn", "uvicorn.error", "fastapi"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    for noisy in (
        "httpx",
        "httpcore",
        "urllib3",
        "websockets",
        "watchfiles",
        "multipart",
        "python_multipart",
        "asyncio",
        "hpack",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def configure_logging(cfg: "Config | None" = None, *, persisted_level: str = "") -> Path:
    """初始化全局日志处理器（幂等，可在服务生命周期启动时安全调用）。"""
    global _ACTIVE_LOG_DIR, _CURRENT_LEVEL_NAME, _MEMORY_HANDLER, _FILE_HANDLERS

    with _LOCK:
        _ensure_utf8_stdio()
        log_dir = resolve_log_dir(cfg)
        log_dir.mkdir(parents=True, exist_ok=True)
        _ACTIVE_LOG_DIR = log_dir
        _prune_housekeeping_files(log_dir, cfg)

        # 确定生效级别：环境变量 > 页面持久化偏好 > config.yaml > info
        env_level = os.environ.get("CREATORHUB_LOG_LEVEL", "").strip().lower()
        cfg_level = str(getattr(getattr(cfg, "logging", None), "level", "info") or "info").strip().lower()
        chosen = (env_level or persisted_level.strip().lower() or cfg_level or "info")
        if chosen not in _LEVEL_MAP:
            chosen = "info"
        _CURRENT_LEVEL_NAME = "warning" if chosen == "warn" else chosen
        numeric_level = _LEVEL_MAP[_CURRENT_LEVEL_NAME]

        max_bytes = int(getattr(getattr(cfg, "logging", None), "max_bytes", 5 * 1024 * 1024) or 5 * 1024 * 1024)
        backup_count = int(getattr(getattr(cfg, "logging", None), "backup_count", 4) or 4)
        err_max_bytes = int(getattr(getattr(cfg, "logging", None), "error_max_bytes", 2 * 1024 * 1024) or 2 * 1024 * 1024)
        err_backup_count = int(getattr(getattr(cfg, "logging", None), "error_backup_count", 3) or 3)

        root = logging.getLogger()
        for existing in list(root.handlers):
            if getattr(existing, _MANAGED_ATTR, False):
                root.removeHandler(existing)
                try:
                    existing.close()
                except Exception:
                    pass
        _FILE_HANDLERS = []

        formatter = CompactRedactingFormatter()
        dedup = BurstDedupFilter(window_seconds=60.0, max_repeats=2)

        handlers: list[logging.Handler] = []

        # 1. 控制台输出（开发终端或桌面端 service-stdio 兜底）
        if sys.stderr is not None:
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setLevel(numeric_level)
            console_handler.setFormatter(formatter)
            console_handler.addFilter(dedup)
            setattr(console_handler, _MANAGED_ATTR, True)
            handlers.append(console_handler)

        # 2. 常规运行日志 logs/app.log（按体积自动滚动）
        try:
            app_file = RotatingFileHandler(
                log_dir / "app.log",
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
                delay=True,
            )
            app_file.setLevel(numeric_level)
            app_file.setFormatter(formatter)
            app_file.addFilter(dedup)
            setattr(app_file, _MANAGED_ATTR, True)
            handlers.append(app_file)
            _FILE_HANDLERS.append(app_file)
        except OSError:
            pass

        # 3. 独立告警与错误日志 logs/error.log（固定记录 WARNING 及以上，不受精简过滤影响）
        try:
            err_file = RotatingFileHandler(
                log_dir / "error.log",
                maxBytes=err_max_bytes,
                backupCount=err_backup_count,
                encoding="utf-8",
                delay=True,
            )
            err_file.setLevel(logging.WARNING)
            err_file.setFormatter(formatter)
            setattr(err_file, _MANAGED_ATTR, True)
            handlers.append(err_file)
            _FILE_HANDLERS.append(err_file)
        except OSError:
            pass

        # 4. 内存环形缓冲（供前端实时检索与一键导出）
        if _MEMORY_HANDLER is None:
            _MEMORY_HANDLER = MemoryLogHandler(capacity=500)
        _MEMORY_HANDLER.setLevel(numeric_level)
        setattr(_MEMORY_HANDLER, _MANAGED_ATTR, True)
        handlers.append(_MEMORY_HANDLER)

        root.setLevel(min(numeric_level, logging.INFO))
        for handler in handlers:
            root.addHandler(handler)

        _quiet_third_party_loggers()
        return log_dir


def shutdown_logging() -> None:
    """优雅关闭并释放所有受管文件句柄（防止 Windows 文件锁残留）。"""
    global _FILE_HANDLERS
    with _LOCK:
        root = logging.getLogger()
        for existing in list(root.handlers):
            if getattr(existing, _MANAGED_ATTR, False) and not isinstance(existing, MemoryLogHandler):
                root.removeHandler(existing)
                try:
                    existing.flush()
                    existing.close()
                except Exception:
                    pass
        _FILE_HANDLERS = []


def get_runtime_log_level() -> str:
    return _CURRENT_LEVEL_NAME


def set_runtime_log_level(level: str) -> str:
    """动态切换运行期日志级别（info / debug / warning），即时生效无需重启。"""
    global _CURRENT_LEVEL_NAME
    normalized = str(level or "").strip().lower()
    if normalized == "warn":
        normalized = "warning"
    if normalized not in {"debug", "info", "warning"}:
        raise ValueError("日志级别仅支持 info、debug 或 warning")
    numeric = _LEVEL_MAP[normalized]
    with _LOCK:
        _CURRENT_LEVEL_NAME = normalized
        root = logging.getLogger()
        root.setLevel(min(numeric, logging.INFO))
        for handler in root.handlers:
            if not getattr(handler, _MANAGED_ATTR, False):
                continue
            if isinstance(handler, RotatingFileHandler) and handler.baseFilename.endswith("error.log"):
                handler.setLevel(logging.WARNING)
            else:
                handler.setLevel(numeric)
    logging.getLogger("creatorhub.logging").info("运行日志级别已切换为 %s", normalized.upper())
    return normalized


def get_memory_handler() -> MemoryLogHandler:
    global _MEMORY_HANDLER
    with _LOCK:
        if _MEMORY_HANDLER is None:
            _MEMORY_HANDLER = MemoryLogHandler(capacity=500)
        return _MEMORY_HANDLER


def log_files_summary(cfg: "Config | None" = None) -> dict[str, Any]:
    log_dir = resolve_log_dir(cfg)
    files_info: list[dict[str, Any]] = []
    total_bytes = 0
    if log_dir.is_dir():
        for p in sorted(log_dir.iterdir(), key=lambda x: x.name):
            if not p.is_file() or not (p.name.endswith(".log") or ".log." in p.name):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            total_bytes += st.st_size
            files_info.append({
                "name": p.name,
                "size_bytes": st.st_size,
                "updated_at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })
    return {
        "log_dir": str(log_dir),
        "level": get_runtime_log_level(),
        "total_bytes": total_bytes,
        "files": files_info,
        "memory_counts": get_memory_handler().counts(),
    }


def read_log_tail(
    path: Path,
    *,
    max_lines: int = 500,
    max_bytes: int = 256 * 1024,
    for_export: bool = False,
) -> str:
    """安全读取日志文件尾部（兼容 Windows 正在写入的句柄，按字节限长）。"""
    if not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        if size <= 0:
            return ""
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]  # 丢弃可能被截断的首行
        if max_lines > 0 and len(lines) > max_lines:
            lines = lines[-max_lines:]
        joined = "\n".join(lines)
        return sanitize_for_export(joined) if for_export else sanitize_log_text(joined)
    except OSError:
        return ""


def clear_log_files(cfg: "Config | None" = None) -> dict[str, Any]:
    """安全清空当前日志与滚动备份文件（Windows 下先释放句柄再截断重开）。"""
    log_dir = resolve_log_dir(cfg)
    removed_files = 0
    with _LOCK:
        for handler in _FILE_HANDLERS:
            handler.acquire()
            try:
                try:
                    handler.close()
                except Exception:
                    pass
                base = Path(handler.baseFilename)
                if base.is_file():
                    try:
                        base.write_bytes(b"")
                        removed_files += 1
                    except OSError:
                        pass
                for backup in log_dir.glob(base.name + ".*"):
                    try:
                        backup.unlink()
                        removed_files += 1
                    except OSError:
                        pass
            finally:
                handler.release()
        # 顺带清理非活跃的历史会话日志（跳过当前进程仍可能占用的最新 1 个）
        for pattern in ("desktop-*.log", "process-*.log", "*.out.log", "*.err.log", "*.stdout.log", "*.stderr.log"):
            for extra in log_dir.glob(pattern):
                try:
                    extra.unlink()
                    removed_files += 1
                except OSError:
                    pass
        if _MEMORY_HANDLER is not None:
            _MEMORY_HANDLER.clear()
    logging.getLogger("creatorhub.logging").info("已手动清理本地历史日志")
    return {"ok": True, "cleared": removed_files}


class RequestLoggingMiddleware:
    """零缓冲纯 ASGI 请求追踪中间件：分配 req_id、静默高频轮询、捕获慢请求与未处理异常。"""

    def __init__(self, app: Any, *, cfg_getter: Any = None) -> None:
        self.app = app
        self._cfg_getter = cfg_getter
        self._http_log = logging.getLogger("creatorhub.http")

    def _display_target(self, scope: dict[str, Any]) -> str:
        path = str(scope.get("path") or "/")
        raw_qs = scope.get("query_string") or b""
        if not raw_qs:
            return path
        try:
            qs = raw_qs.decode("latin-1")
        except Exception:
            return path
        cleaned = _clean_single_url(f"http://localhost{path}?{qs}")
        parsed = urlsplit(cleaned)
        return f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        req_id = uuid.uuid4().hex[:8]
        for k, v in scope.get("headers") or ():
            if k.lower() == b"x-request-id":
                candidate = v.decode("latin-1", errors="ignore").strip()
                if candidate and len(candidate) <= 36 and candidate.replace("-", "").isalnum():
                    req_id = candidate
                break

        token = request_id_var.set(req_id)
        method = str(scope.get("method") or "GET").upper()
        path = str(scope.get("path") or "/")
        started = time.monotonic()
        status_code = 500
        response_started = False

        async def wrapped_send(message: dict[str, Any]) -> None:
            nonlocal status_code, response_started
            if message.get("type") == "http.response.start":
                response_started = True
                status_code = int(message.get("status", 200))
                headers = list(message.get("headers") or [])
                if not any(k.lower() == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", req_id.encode("ascii", errors="ignore")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        except Exception:
            duration_ms = int((time.monotonic() - started) * 1000)
            target = self._display_target(scope)
            self._http_log.exception(
                "未处理接口异常 %s %s (%dms)", method, target, duration_ms
            )
            raise
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            if response_started:
                cfg = self._cfg_getter() if callable(self._cfg_getter) else None
                slow_ms = int(getattr(getattr(cfg, "logging", None), "slow_request_ms", 3000) or 0)
                log_mutations = bool(getattr(getattr(cfg, "logging", None), "log_mutations", True))
                is_stream = any(h in path for h in _STREAMING_PATH_HINTS)
                is_Slow = bool(slow_ms > 0 and duration_ms >= slow_ms and not is_stream)

                if status_code >= 500:
                    self._http_log.error(
                        "%s %s -> %d (%dms)",
                        method, self._display_target(scope), status_code, duration_ms,
                    )
                elif status_code >= 400 and not (status_code == 404 and path == "/favicon.ico"):
                    self._http_log.warning(
                        "%s %s -> %d (%dms)",
                        method, self._display_target(scope), status_code, duration_ms,
                    )
                elif is_Slow:
                    self._http_log.warning(
                        "慢请求 %s %s -> %d (%dms)",
                        method, self._display_target(scope), status_code, duration_ms,
                    )
                elif (
                    log_mutations
                    and method in {"POST", "PUT", "PATCH", "DELETE"}
                    and not path.startswith("/api/diagnostics/")
                ):
                    self._http_log.info(
                        "%s %s -> %d (%dms)",
                        method, self._display_target(scope), status_code, duration_ms,
                    )
            request_id_var.reset(token)
