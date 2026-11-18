"""Local self-signed CA and leaf TLS context management for selective Channels proxy."""
from __future__ import annotations

import datetime
import locale
import os
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ALLOWED_INTERCEPT_HOSTS = frozenset({"channels.weixin.qq.com"})
CA_COMMON_NAME = "CreatorHub Channels Local CA"


def _write_private_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _creation_flags() -> int:
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _windows_code_pages() -> list[str]:
    """Return the system OEM then ANSI code page; piped console tools such as certutil write OEM."""
    if sys.platform != "win32":
        return []
    pages: list[str] = []
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for cp in (int(kernel32.GetOEMCP()), int(kernel32.GetACP())):
            if cp and f"cp{cp}" not in pages:
                pages.append(f"cp{cp}")
    except Exception:
        pass
    return pages


def _decode_windows_output(raw: bytes | str | None) -> str:
    """Decode `certutil` stdout/stderr cleanly across UTF-8 and Windows OEM/ACP (e.g. CP936)."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass
    candidates = _windows_code_pages()
    if sys.platform == "win32":
        candidates.append("mbcs")
    preferred = locale.getpreferredencoding(False)
    if preferred:
        candidates.append(preferred)
    for enc in (*candidates, "utf-8"):
        try:
            return raw.decode(enc, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    fallback_enc = candidates[0] if candidates else "utf-8"
    return raw.decode(fallback_enc, errors="replace")


def _run_certutil(
    args: list[str],
    *,
    timeout: float = 10.0,
    hide_without_no_window: bool = False,
) -> tuple[int, str, str]:
    """Run `certutil.exe` and return `(returncode, stdout, stderr)` with clean encoding."""
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "timeout": timeout,
    }
    if sys.platform == "win32":
        if hide_without_no_window:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE: hides console without detaching session/UI context
            kwargs["startupinfo"] = si
            kwargs["creationflags"] = 0
        else:
            kwargs["creationflags"] = _creation_flags()
    res = subprocess.run(["certutil", *args], **kwargs)
    return (
        int(res.returncode),
        _decode_windows_output(res.stdout),
        _decode_windows_output(res.stderr),
    )


def _run_elevated_certutil(args: list[str], *, timeout_ms: int = 60000) -> tuple[bool, int, str]:
    """Invoke `certutil` via Windows standard UAC elevation prompt (`ShellExecuteExW` runas)."""
    if sys.platform != "win32":
        return False, 1, "仅 Windows 支持管理员 UAC 提权安装"
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    ERROR_CANCELLED = 1223

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.hwnd = None
    sei.lpVerb = "runas"
    sei.lpFile = "certutil.exe"
    sei.lpParameters = subprocess.list2cmdline(args)
    sei.lpDirectory = None
    sei.nShow = 0  # SW_HIDE

    if not shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            return False, ERROR_CANCELLED, "已取消系统管理员授权（UAC）弹窗，未能完成证书安装"
        return False, err or 1, f"请求系统管理员授权失败（Win32={err}）"

    handle = sei.hProcess
    if not handle:
        return True, 0, ""
    try:
        wait_res = kernel32.WaitForSingleObject(handle, int(timeout_ms))
        if wait_res == WAIT_TIMEOUT:
            return False, WAIT_TIMEOUT, "等待管理员证书命令执行超时"
        if wait_res != WAIT_OBJECT_0:
            return False, 1, "等待管理员证书命令执行异常"
        exit_code = wintypes.DWORD(1)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False, 1, "未能读取管理员证书命令退出状态"
        code = int(exit_code.value)
        return code == 0, code, "" if code == 0 else f"管理员 certutil 退出码 {code}"
    finally:
        kernel32.CloseHandle(handle)


def _needs_elevation_fallback(detail: str) -> bool:
    upper = (detail or "").upper()
    return any(
        marker in upper
        for marker in (
            "0X80070542",
            "WIN32: 1346",
            "ERROR_BAD_IMPERSONATION_LEVEL",
            "0X80070005",
            "WIN32: 5 ",
            "E_ACCESSDENIED",
            "ERROR_ACCESS_DENIED",
        )
    )


class LocalCaManager:
    """生成与管理仅用于本机 `channels.weixin.qq.com` 桥接的自签名 CA 与单域名叶子证书。"""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = Path(data_dir or "data").expanduser()
        self._lock = threading.Lock()
        self._leaf_dir: tempfile.TemporaryDirectory[str] | None = None
        self._contexts: dict[str, ssl.SSLContext] = {}

    def configure_data_dir(self, data_dir: Path) -> None:
        with self._lock:
            new_dir = Path(data_dir).expanduser()
            if new_dir != self._data_dir:
                self._data_dir = new_dir
                self._clear_leaf_cache_locked()

    @property
    def ca_cert_path(self) -> Path:
        return (self._data_dir / "channels_proxy_ca.cer").resolve()

    @property
    def ca_key_path(self) -> Path:
        return (self._data_dir / "channels_proxy_ca.key").resolve()

    def _clear_leaf_cache_locked(self) -> None:
        self._contexts.clear()
        if self._leaf_dir is not None:
            try:
                self._leaf_dir.cleanup()
            except Exception:
                pass
            self._leaf_dir = None

    def close(self) -> None:
        with self._lock:
            self._clear_leaf_cache_locked()

    def _load_existing_ca_locked(self) -> tuple[x509.Certificate, rsa.RSAPrivateKey] | None:
        cert_path = self.ca_cert_path
        key_path = self.ca_key_path
        if not cert_path.is_file() or not key_path.is_file():
            return None
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
            if not isinstance(key, rsa.RSAPrivateKey):
                return None
            now = datetime.datetime.now(datetime.timezone.utc)
            expiry = getattr(cert, "not_valid_after_utc", None)
            if expiry is None:
                expiry = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
            if expiry <= now + datetime.timedelta(days=30):
                return None
            return cert, key
        except Exception:
            return None

    def _ensure_ca_locked(self) -> tuple[x509.Certificate, rsa.RSAPrivateKey, str]:
        existing = self._load_existing_ca_locked()
        if existing is not None:
            cert, key = existing
            thumb = cert.fingerprint(hashes.SHA1()).hex().upper()
            return cert, key, thumb

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.timezone.utc)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "CreatorHub Local"),
            x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME),
        ])
        ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(ski, critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        _write_private_file(self.ca_cert_path, cert_pem)
        _write_private_file(self.ca_key_path, key_pem)
        self._clear_leaf_cache_locked()
        thumb = cert.fingerprint(hashes.SHA1()).hex().upper()
        return cert, key, thumb

    def ensure_ca(self) -> tuple[Path, Path, str]:
        with self._lock:
            _, _, thumb = self._ensure_ca_locked()
            return self.ca_cert_path, self.ca_key_path, thumb

    def ssl_context_for_host(self, hostname: str) -> ssl.SSLContext:
        host = (hostname or "").strip().lower()
        if host not in ALLOWED_INTERCEPT_HOSTS:
            raise ValueError(f"拒绝签发非允许域名的 TLS 证书: {hostname}")

        with self._lock:
            cached = self._contexts.get(host)
            if cached is not None:
                return cached

            ca_cert, ca_key, _ = self._ensure_ca_locked()
            leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            now = datetime.datetime.now(datetime.timezone.utc)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
            leaf_cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(ca_cert.subject)
                .public_key(leaf_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=365))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=True,
                        content_commitment=False,
                        key_encipherment=True,
                        data_encipherment=False,
                        key_agreement=False,
                        key_cert_sign=False,
                        crl_sign=False,
                        encipher_only=False,
                        decipher_only=False,
                    ),
                    critical=True,
                )
                .add_extension(
                    x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                    critical=False,
                )
                .add_extension(
                    x509.SubjectAlternativeName([x509.DNSName(host)]),
                    critical=False,
                )
                .add_extension(
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                    critical=False,
                )
                .sign(ca_key, hashes.SHA256())
            )
            if self._leaf_dir is None:
                self._leaf_dir = tempfile.TemporaryDirectory(prefix="ch-leaf-")
            leaf_dir = Path(self._leaf_dir.name)
            cert_file = leaf_dir / f"{host}.crt"
            key_file = leaf_dir / f"{host}.key"
            _write_private_file(cert_file, leaf_cert.public_bytes(serialization.Encoding.PEM))
            _write_private_file(
                key_file,
                leaf_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            )
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
            ctx.set_alpn_protocols(["http/1.1"])
            self._contexts[host] = ctx
            return ctx

    def trust_status(self) -> dict[str, Any]:
        with self._lock:
            existing = self._load_existing_ca_locked()
            if existing is None:
                return {
                    "exists": False,
                    "thumbprint": "",
                    "trusted": False,
                    "store": "",
                    "cert_path": str(self.ca_cert_path),
                    "common_name": CA_COMMON_NAME,
                }
            cert, _ = existing
            thumb = cert.fingerprint(hashes.SHA1()).hex().upper()

        trusted = False
        store = ""
        if sys.platform == "win32":
            for candidate_store, args in (
                ("CurrentUser\\Root", ["-user", "-store", "Root", thumb]),
                ("LocalMachine\\Root", ["-store", "Root", thumb]),
            ):
                try:
                    rc, out, _ = _run_certutil(args, timeout=8.0)
                    if rc == 0 and thumb.lower() in out.replace(" ", "").lower():
                        trusted = True
                        store = candidate_store
                        break
                except Exception:
                    continue
        return {
            "exists": True,
            "thumbprint": thumb,
            "trusted": trusted,
            "store": store,
            "cert_path": str(self.ca_cert_path),
            "common_name": CA_COMMON_NAME,
        }

    def install_trust(self) -> dict[str, Any]:
        cert_path, _, _ = self.ensure_ca()
        status = self.trust_status()
        if status["trusted"]:
            return {"ok": True, **status, "message": f"本机根证书已受信任（{status.get('store') or 'Root'}）"}
        if sys.platform != "win32":
            return {
                "ok": False,
                **status,
                "error": f"当前系统不支持自动安装根证书，请手动信任证书文件: {cert_path}",
            }
        try:
            rc, out, err = _run_certutil(
                ["-user", "-addstore", "Root", str(cert_path)],
                timeout=60.0,
                hide_without_no_window=True,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                **self.trust_status(),
                "error": "等待系统根证书确认弹窗超时，请重试并在弹窗中点击「是」",
            }
        except Exception as exc:
            return {"ok": False, **self.trust_status(), "error": f"调用 certutil 安装证书失败: {exc}"}

        after = self.trust_status()
        if after["trusted"]:
            installed_store = after.get("store") or "CurrentUser\\Root"
            return {"ok": True, **after, "message": f"本地 CA 根证书已安装并受信任（{installed_store}）"}

        detail = "\n".join(part for part in (out.strip(), err.strip()) if part)
        if rc != 0 and _needs_elevation_fallback(detail):
            ok_uac, _, uac_err = _run_elevated_certutil(["-f", "-addstore", "Root", str(cert_path)])
            after = self.trust_status()
            if after["trusted"]:
                installed_store = after.get("store") or "LocalMachine\\Root"
                return {
                    "ok": True,
                    **after,
                    "message": f"已通过系统管理员授权安装本地 CA 根证书（{installed_store}）",
                }
            if uac_err:
                detail = f"{uac_err}；也可点击「下载证书」手动双击导入「受信任的根证书颁发机构」（{detail}）"
            elif not ok_uac:
                detail = f"未能通过管理员授权写入本机根证书存储；可点击「下载证书」手动双击导入（{detail}）"

        return {
            "ok": False,
            **after,
            "error": detail or "根证书安装未完成（可能取消了系统安全提示弹窗）",
        }

    def uninstall_trust(self) -> dict[str, Any]:
        status = self.trust_status()
        thumb = status.get("thumbprint") or ""
        if not thumb or not status["trusted"]:
            return {"ok": True, **status, "message": "本地根证书当前未安装"}
        if sys.platform != "win32":
            return {"ok": False, **status, "error": "当前系统不支持自动卸载根证书"}

        store = str(status.get("store") or "")
        detail = ""
        try:
            if store == "CurrentUser\\Root" or not store:
                rc, out, err = _run_certutil(["-user", "-delstore", "Root", thumb], timeout=30.0)
                if rc != 0:
                    detail = "\n".join(part for part in (out.strip(), err.strip()) if part)
            after = self.trust_status()
            if after["trusted"] and after.get("store") == "LocalMachine\\Root":
                rc2, out2, err2 = _run_certutil(["-delstore", "Root", thumb], timeout=30.0)
                if rc2 != 0:
                    detail2 = "\n".join(part for part in (out2.strip(), err2.strip()) if part)
                    if _needs_elevation_fallback(detail2):
                        ok_uac, _, uac_err = _run_elevated_certutil(["-delstore", "Root", thumb])
                        if not ok_uac and uac_err:
                            detail = uac_err
                    else:
                        detail = detail2
        except Exception as exc:
            return {"ok": False, **self.trust_status(), "error": f"卸载根证书失败: {exc}"}

        after = self.trust_status()
        if not after["trusted"]:
            return {"ok": True, **after, "message": "已从本机受信任根证书存储卸载"}
        return {"ok": False, **after, "error": detail or "未能卸载根证书（若安装在本地计算机存储区需在 UAC 弹窗确认管理员权限）"}
