"""Versioned file-update contract shared by the publisher and frozen updater.

Only application-owned files are accepted; manifests never name user-data or
absolute paths. Transport authenticity comes from the same GitHub Release's
SHA256.txt/asset digest, not from an invented local signing key.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat

MAX_MANIFEST_SIZE = 4 * 1024 ** 2
MAX_FILE_SIZE = 2 * 1024 ** 3
MAX_TREE_SIZE = 4 * 1024 ** 3


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,8}\.\d{1,8}\.\d{1,8}(?:\.\d{1,8})?", value):
        raise ValueError("Invalid manifest version")
    parts = tuple(map(int, value.split(".")))
    return parts + (0,) * (4 - len(parts))


def manifest_name(version):
    version_tuple(version)
    return f"CreatorHub-Update-{version}-windows-x64.json"


def delta_name(base, version):
    version_tuple(base)
    version_tuple(version)
    return f"CreatorHub-Delta-{base}-to-{version}-windows-x64.zip"


def valid_digest(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[a-f0-9]{64}", value))


def file_path(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 240:
        raise ValueError("Invalid application file path")
    parts = value.split("/")
    reserved = r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?"
    if (str(PurePosixPath(value)) != value or any(
            not p or p in {".", ".."} or p.endswith((".", " "))
            or re.search(r'[<>:"\\|?*\x00-\x1f]', p) or re.fullmatch(reserved, p)
            for p in parts)
            or not (value == "CreatorHub.exe" or (len(parts) > 1 and parts[0] == "_internal"))):
        raise ValueError("Manifest names a non-application path")
    return value


def safe_path(root, relative):
    """Reject junctions/reparse points as well as Unix symlinks on every component."""
    root = Path(root).absolute()
    file_path(relative)
    path = root
    for part in ("", *relative.split("/")):
        if part:
            path /= part
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Reparse points are not update targets")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Application path escaped installation")
    return path


def hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_files(files):
    if not isinstance(files, list) or not 2 <= len(files) <= 20000:
        raise ValueError("Invalid file inventory")
    seen, total = set(), 0
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise ValueError("Invalid file inventory entry")
        name = file_path(entry["path"]).casefold()
        if name in seen or type(entry["size"]) is not int or not 0 <= entry["size"] <= MAX_FILE_SIZE or not valid_digest(entry["sha256"]):
            raise ValueError("Invalid or duplicate application file")
        seen.add(name)
        total += entry["size"]
    if total > MAX_TREE_SIZE or not {"creatorhub.exe", "_internal/desktop-version.txt"} <= seen:
        raise ValueError("Incomplete or oversized application inventory")
    for name in seen:
        if any("/".join(name.split("/")[:i]) in seen for i in range(1, len(name.split("/")))):
            raise ValueError("File/directory collision in inventory")
    return files


def changes(base, target):
    old = {f["path"]: f for f in base}
    new = {f["path"]: f for f in target}
    return [f for f in target if old.get(f["path"]) != f], sorted(old.keys() - new.keys())


def validate_manifest(data, version=None):
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("platform") != "windows-x64":
        raise ValueError("Unsupported update manifest")
    version_tuple(data.get("version"))
    if version is not None and data["version"] != version:
        raise ValueError("Manifest release version mismatch")
    if not valid_digest(data.get("runtime_id")):
        raise ValueError("Missing runtime compatibility identity")
    validate_files(data.get("files"))
    delta = data.get("delta")
    if delta is not None:
        if not isinstance(delta, dict) or version_tuple(delta.get("from_version")) >= version_tuple(data["version"]):
            raise ValueError("Invalid delta base")
        validate_files(delta.get("base_files"))
        if (delta.get("asset_name") != delta_name(delta["from_version"], data["version"])
                or type(delta.get("size")) is not int or not 0 < delta["size"] <= MAX_FILE_SIZE
                or not valid_digest(delta.get("sha256"))):
            raise ValueError("Invalid delta asset")
        # Avoid case-only renames and file/directory changes in the initial protocol.
        validate_files(list({f["path"]: f for f in delta["base_files"] + data["files"]}.values()))
    return data


def parse_manifest(raw, version=None):
    if len(raw) > MAX_MANIFEST_SIZE:
        raise ValueError("Update manifest too large")
    return validate_manifest(json.loads(raw.decode("utf-8-sig")), version)


def verify_tree(root, files, cancelled=lambda: None):
    for entry in files:
        cancelled()
        path = safe_path(root, entry["path"])
        if not path.is_file() or path.stat().st_size != entry["size"] or hash_file(path) != entry["sha256"]:
            raise ValueError("Local application files do not match the delta baseline")


def inventory(root):
    root = Path(root).resolve(strict=True)
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            name = path.relative_to(root).as_posix()
            safe_path(root, name)
            result.append({"path": name, "size": path.stat().st_size, "sha256": hash_file(path)})
    validate_files(result)
    return result
