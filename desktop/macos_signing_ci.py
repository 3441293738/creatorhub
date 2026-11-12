"""Import signing material only into an ephemeral GitHub macOS runner keychain."""
import base64
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys


def run(command, **kwargs):
    # CalledProcessError embeds argv, which could expose an import password in
    # CI logs. Keep tool output private and report only the operation name.
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode:
        raise SystemExit(f"Signing credential setup failed: {command[0]} {command[1]}")
    return result


def main():
    if sys.platform != "darwin" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("This helper is restricted to ephemeral GitHub macOS runners")
    root = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    folder = root / "creatorhub-signing"
    folder.mkdir(mode=0o700, exist_ok=True)
    keychain = folder / "build.keychain-db"
    prior = folder / "keychains.json"
    if sys.argv[1] == "cleanup":
        if prior.exists():
            run(["security", "list-keychains", "-d", "user", "-s", *json.loads(prior.read_text())])
        if keychain.exists():
            run(["security", "delete-keychain", str(keychain)])
        # Only the fixed files created below; no recursive deletion of computed paths.
        for name in ("certificate.p12", "AuthKey.p8", "keychains.json"):
            (folder / name).unlink(missing_ok=True)
        return
    if sys.argv[1] != "prepare":
        raise SystemExit("Expected prepare or cleanup")
    required = ("MACOS_CERTIFICATE_P12_BASE64", "MACOS_CERTIFICATE_PASSWORD", "MACOS_SIGNING_IDENTITY",
                "APPLE_API_KEY_P8_BASE64", "APPLE_API_KEY_ID", "APPLE_API_ISSUER_ID")
    if any(not os.environ.get(name) for name in required):
        raise SystemExit("Configure all six macOS signing secrets before enabling signed builds")
    certificate, api_key = folder / "certificate.p12", folder / "AuthKey.p8"
    for path, variable in ((certificate, required[0]), (api_key, required[3])):
        path.write_bytes(base64.b64decode(os.environ[variable], validate=True))
        path.chmod(0o600)
    password = secrets.token_urlsafe(32)
    previous = shlex.split(subprocess.check_output(["security", "list-keychains", "-d", "user"], text=True))
    prior.write_text(json.dumps(previous))
    run(["security", "create-keychain", "-p", password, str(keychain)])
    run(["security", "set-keychain-settings", "-lut", "21600", str(keychain)])
    run(["security", "unlock-keychain", "-p", password, str(keychain)])
    run(["security", "import", str(certificate), "-P", os.environ[required[1]], "-k", str(keychain),
                    "-T", "/usr/bin/codesign", "-T", "/usr/bin/security"])
    run(["security", "set-key-partition-list", "-S", "apple-tool:,apple:", "-k", password, str(keychain)])
    run(["security", "list-keychains", "-d", "user", "-s", str(keychain), *previous])
    run(["xcrun", "notarytool", "store-credentials", "creatorhub", "--key", str(api_key),
                    "--key-id", os.environ[required[4]], "--issuer", os.environ[required[5]],
                    "--keychain", str(keychain)])
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as stream:
        stream.write(f"MACOS_NOTARY_KEYCHAIN={keychain}\n")
    certificate.unlink()
    api_key.unlink()


if __name__ == "__main__":
    main()
