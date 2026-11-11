from __future__ import annotations
import base64
import json
import time
from pathlib import Path
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from .settings import get_setting, set_setting

KEY = 'commercial_license'
PUBLIC_KEY = Path(__file__).with_name('license_public.pem')


def decode(code: str) -> dict:
    if not isinstance(code, str) or len(code) > 16384:
        raise ValueError('激活码格式错误')
    try:
        payload, signature = code.strip().split('.')
        raw = base64.b64decode(payload, altchars=b'-_', validate=True)
        sig = base64.b64decode(signature, altchars=b'-_', validate=True)
        load_pem_public_key(PUBLIC_KEY.read_bytes()).verify(sig, raw)
        data = json.loads(raw)
        if (data.get('product') != 'huoke-assistant-v1' or
            type(data.get('expires')) is not int or type(data.get('issued')) is not int or
            data['expires'] <= data['issued'] or not isinstance(data.get('customer'), str)):
            raise ValueError()
        return data
    except Exception as exc:
        raise ValueError('激活码无效或已被篡改') from exc


def status(code=None, now=None):
    from datetime import datetime, timezone
    code = get_setting(KEY) if code is None else code
    result = dict(status='inactive', valid=False, expires_at=None, customer='')
    if not code:
        return result
    try:
        data = decode(code)
        now = time.time() if now is None else now
        result.update(expires_at=datetime.fromtimestamp(data['expires'], timezone.utc).isoformat(), customer=data['customer'])
        result['status'] = 'expired' if now >= data['expires'] else ('invalid' if now < data['issued'] else 'active')
        result['valid'] = result['status'] == 'active'
    except (ValueError, OverflowError, OSError):
        result['status'] = 'invalid'
    return result


def is_active():
    return status()['valid']


def activate(code):
    decode(code)
    result = status(code)
    if not result['valid']:
        raise ValueError('激活码已到期或尚未生效')
    set_setting(KEY, code.strip())
    return result


class LicenseRequired(PermissionError):
    pass


def require_active():
    if not is_active():
        raise LicenseRequired('授权未激活或已到期，请激活或续期')
