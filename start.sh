#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    exec python3 creatorhub.py "$@"
fi

if command -v python >/dev/null 2>&1; then
    exec python creatorhub.py "$@"
fi

if [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python creatorhub.py "$@"
fi

echo "[CreatorHub] 需要先安装 Python 3.10+。" >&2
exit 1
