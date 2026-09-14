"""Stage a complete verified tree, swap directories, and restore on failure.

Never merge files into a running installation. The helper owns both runtime
locks. A durable journal lets the external helper restore an interrupted swap;
user data and database migrations are outside this transaction.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile

from desktop.update_manifest import (changes, hash_file, parse_manifest, safe_path,
                                    verify_tree)


def atomic_record(path, data):
    path = Path(path)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as output:
        json.dump(data, output)
        output.flush()
        os.fsync(output.fileno())
    temp.replace(path)


def load_delta(data):
    raw = Path(data["manifest"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != data["manifest_sha256"]:
        raise ValueError("Staged manifest checksum mismatch")
    manifest = parse_manifest(raw, data["version"])
    delta = manifest.get("delta")
    if (not delta or delta["from_version"] != data["from_version"]
            or delta["asset_name"] != Path(data["installer"]).name
            or delta["size"] != data["size"] or delta["sha256"] != data["sha256"]):
        raise ValueError("Delta request does not match the release manifest")
    return manifest


def prepare_tree(data, manifest, candidate):
    app = data["install_dir"]
    verify_tree(app, manifest["delta"]["base_files"])
    total = sum(f["size"] for f in manifest["files"])
    if shutil.disk_usage(app.parent).free < total + 64 * 1024 ** 2:
        raise ValueError("Not enough space to stage a complete application")
    candidate.mkdir()
    changed, _ = changes(manifest["delta"]["base_files"], manifest["files"])
    entries = {f["path"]: f for f in changed}
    with zipfile.ZipFile(data["installer"]) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(entries):
            raise ValueError("Delta contains missing, duplicate or unexpected files")
        for info in archive.infolist():
            entry = entries[info.filename]
            if (info.is_dir() or stat.S_ISLNK(info.external_attr >> 16)
                    or info.flag_bits & 1 or info.file_size != entry["size"]
                    or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise ValueError("Invalid delta archive entry")
            target = safe_path(candidate, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with archive.open(info) as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > entry["size"]:
                        raise ValueError("Delta entry exceeded expected size")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
    for entry in manifest["files"]:
        if entry["path"] not in entries:
            target = safe_path(candidate, entry["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(safe_path(app, entry["path"]), target)
    # Preserve the existing Inno uninstaller, not arbitrary mutable files.
    # Other files remain intact in the previous directory, never deleted.
    for path in app.iterdir():
        if re.fullmatch(r"unins\d{3}\.(exe|dat|msg)", path.name, re.I):
            if (path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400
                    or not path.is_file() or path.stat().st_size > 32 * 1024 ** 2):
                raise ValueError("Invalid installed uninstaller")
            shutil.copy2(path, candidate / path.name)
    verify_tree(candidate, manifest["files"])
    if (candidate / "_internal/desktop-version.txt").read_text(encoding="utf-8").strip() != data["version"]:
        raise ValueError("Target version file is inconsistent")


def health_check(app, environment):
    # No account DB, browser launch, service, migrations or network in this mode.
    with tempfile.TemporaryDirectory(prefix="creatorhub-update-health-") as temp:
        env = {**environment, "CREATORHUB_DESKTOP_HOME": temp,
               "CREATORHUB_CONFIG_PATH": str(Path(temp) / "absent.yaml")}
        with (Path(temp) / "health.log").open("wb") as log:
            subprocess.run([str(app / "CreatorHub.exe"), "--update-health-check"],
                           cwd=temp, env=env, stdout=log, stderr=log,
                           check=True, timeout=90,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def read_journal(home):
    path = Path(home) / "runtime/update-transaction.json"
    if not path.is_file():
        return None
    if path.stat().st_size > 8192:
        raise ValueError("Invalid recovery journal")
    data = json.loads(path.read_text(encoding="utf-8"))
    attempt = data.get("attempt", "")
    if data.get("schema") != 1 or not re.fullmatch(r"update-[a-f0-9]{32}", attempt):
        raise ValueError("Invalid recovery identity")
    for name in ("install_dir", "candidate", "previous"):
        if not isinstance(data.get(name), str) or not Path(data[name]).is_absolute():
            raise ValueError("Invalid recovery path")
        data[name] = Path(data[name])
        if data[name].is_symlink() or (data[name].exists() and getattr(data[name].lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Recovery does not follow links")
    app = data["install_dir"]
    if (data["candidate"] != app.parent / (".CreatorHub-next-" + attempt)
            or data["previous"] != app.parent / (".CreatorHub-previous-" + attempt)
            or Path(home).resolve().is_relative_to(app.resolve())):
        raise ValueError("Recovery paths are outside the installation transaction")
    return data


def rollback(home, journal):
    app, previous = journal["install_dir"], journal["previous"]
    if previous.is_dir():
        if app.exists():
            failed = app.parent / (".CreatorHub-failed-" + journal["attempt"])
            if failed.exists():
                raise ValueError("Recovery destination already exists")
            app.rename(failed)
        previous.rename(app)
    elif not app.is_dir():
        raise ValueError("No previous installation available")
    record = {k: str(v) if isinstance(v, Path) else v for k, v in journal.items()}
    record["phase"] = "rolled_back"
    atomic_record(Path(home) / "runtime/update-transaction.json", record)


def update_display_version(data):
    """Only update this per-user Inno entry if it names this exact installation."""
    if os.name != "nt":
        return
    import winreg
    name = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{93FD4B37-6436-4CE0-8249-BC487CC5A062}_is1"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, name, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            location, _ = winreg.QueryValueEx(key, "InstallLocation")
            if Path(location).resolve() == data["install_dir"].resolve():
                winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, data["version"])
    except OSError:
        pass  # Portable bundles have no installer registry entry.


def apply_delta(data, environment):
    manifest = load_delta(data)
    app, attempt = data["install_dir"], data["attempt"]
    candidate = app.parent / (".CreatorHub-next-" + attempt)
    previous = app.parent / (".CreatorHub-previous-" + attempt)
    if candidate.exists() or previous.exists():
        raise ValueError("Update attempt has already staged or replaced files")
    prior = read_journal(data["home"])
    if prior and prior.get("phase") not in {"committed", "rolled_back"}:
        raise ValueError("An interrupted transaction must be restored first")
    prepare_tree(data, manifest, candidate)
    health_check(candidate, environment)
    record = {"schema": 1, "attempt": attempt, "phase": "prepared", "version": data["version"],
              "install_dir": str(app), "candidate": str(candidate), "previous": str(previous)}
    journal_path = data["home"] / "runtime/update-transaction.json"
    atomic_record(journal_path, record)
    try:
        app.rename(previous)
        atomic_record(journal_path, {**record, "phase": "old_saved"})
        candidate.rename(app)
        atomic_record(journal_path, {**record, "phase": "switched"})
        health_check(app, environment)
        atomic_record(journal_path, {**record, "phase": "committed"})
    except Exception:
        rollback(data["home"], read_journal(data["home"]))
        raise
    update_display_version(data)
