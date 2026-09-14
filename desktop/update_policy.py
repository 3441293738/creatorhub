"""Small, persistent reminder policy; never downloads or installs anything.

The UpdateChecker owns synchronization. Corrupt settings fall back to defaults;
failed automatic timestamp writes still throttle this session.
"""
import json
import math
from pathlib import Path

CHECK_INTERVAL = 24 * 60 * 60


class UpdatePolicy:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.data = {"auto_check": True, "last_check": 0, "skipped_tag": "",
                     "deferred_tag": "", "deferred_until": 0}
        try:
            if not self.path or self.path.stat().st_size > 8192:
                return
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                return
            if type(saved.get("auto_check")) is bool:
                self.data["auto_check"] = saved["auto_check"]
            for key in ("last_check", "deferred_until"):
                value = saved.get(key)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    self.data[key] = value
            for key in ("skipped_tag", "deferred_tag"):
                value = saved.get(key)
                if isinstance(value, str) and len(value) <= 64:
                    self.data[key] = value
        except (OSError, ValueError, OverflowError):
            pass

    def save(self, *, best_effort=False, **changes):
        data = {**self.data, **changes}
        try:
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_suffix(".tmp")
                temp.write_text(json.dumps(data), encoding="utf-8")
                temp.replace(self.path)
        except OSError:
            if not best_effort:
                raise ValueError("更新偏好未能保存，请检查用户目录是否可写。") from None
        self.data = data

    def due(self, now):
        last = self.data["last_check"]
        # A clock rollback must not suppress checks indefinitely.
        return self.data["auto_check"] and (last == 0 or now < last or now - last >= CHECK_INTERVAL)

    def suppressed(self, tag, now):
        return bool(tag) and (tag == self.data["skipped_tag"] or (
            tag == self.data["deferred_tag"] and now < self.data["deferred_until"] <= now + CHECK_INTERVAL))

    def dismiss(self, tag, now, *, skip=False):
        if skip:
            self.save(skipped_tag=tag, deferred_tag="", deferred_until=0)
        else:
            self.save(deferred_tag=tag, deferred_until=now + CHECK_INTERVAL)
