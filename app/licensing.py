from __future__ import annotations
import base64
import ctypes
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from .settings import get_setting, set_setting

KEY = 'device_license_credential'
PUBLIC_KEY = Path(__file__).with_name('license_public.pem')
SERVICE_URL = os.environ.get('CREATORHUB_LICENSE_URL', 'http://3.38.149.16:8088').rstrip('/')


def device_digest():
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography', 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        value = winreg.QueryValueEx(key, 'MachineGuid')[0]
    if not isinstance(value, str) or not value.strip():
        raise ValueError('无法读取设备标识，请联系管理员')
    return hashlib.sha256(('creatorhub-device-v1:' + value.strip().lower()).encode()).hexdigest()


def clock():
    # Windows interrupt time includes suspend; unbiased time excludes suspend.
    if os.name == 'nt':
        awake = ctypes.c_ulonglong()
        tick_count = ctypes.windll.kernel32.GetTickCount64
        tick_count.restype = ctypes.c_ulonglong
        total = tick_count()
        if not ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(awake)):
            raise OSError('无法读取运行计时器')
        return total / 1000, awake.value / 1e7
    return time.monotonic(), time.monotonic()


def transport(path, body):
    req = urllib.request.Request(SERVICE_URL + path, json.dumps(body).encode(), {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read(32768))


class LicenseClient:
    def __init__(self, device=device_digest, timer=clock, request=transport, public_key=None, read=get_setting, write=set_setting):
        self.device, self.timer, self.request = device, timer, request
        self.public_key, self.read, self.write = public_key, read, write
        self.lock = threading.RLock()
        self.deadline = 0
        self.last = None
        self.next_check = 0
        self.needs_resume = False
        self.authorized_device = None
        self.data = dict(status='inactive', valid=False, expires_at=None, customer='', device_digest='', reason='启动时需要联网校验')
        self.event = threading.Event()
        self.thread = None

    def _time(self):
        current = self.timer()
        if self.last is not None:
            elapsed, awake = current[0]-self.last[0], current[1]-self.last[1]
            if elapsed < 0 or awake < 0 or abs(elapsed-awake) > 1:
                self.deadline = 0
                self.needs_resume = True
                self.data.update(status='offline', reason='休眠恢复或计时状态改变，请联网重新校验')
        self.last = current
        return current[0]

    def status(self):
        with self.lock:
            try:
                now = self._time()
                self.data['device_digest'] = self.device()
                self.data['valid'] = now < self.deadline and self.data['device_digest'] == self.authorized_device
            except Exception:
                self.deadline = 0
                self.data.update(valid=False, status='invalid', reason='无法读取设备标识或计时器')
            if not self.data['valid'] and self.data['status'] == 'active':
                self.data.update(status='expired', reason='授权或离线宽限已结束，请重新校验')
            return dict(self.data)

    def validate(self, code=None):
        with self.lock:
            if code is not None:
                self.deadline = 0
            try:
                start = self._time()
                device = self.device()
                nonce = secrets.token_urlsafe(32)
                saved = {} if code is not None else json.loads(self.read(KEY) or '{}')
                credential = saved.get('credential', '')
                body = dict(device=device, nonce=nonce)
                body.update(code=code) if code is not None else body.update(credential=credential)
                response = self.request('/v1/activate' if code is not None else '/v1/check', body)
                raw = base64.urlsafe_b64decode(response['payload'] + '=' * (-len(response['payload']) % 4))
                sig = base64.urlsafe_b64decode(response['signature'] + '=' * (-len(response['signature']) % 4))
                key = self.public_key or load_pem_public_key(PUBLIC_KEY.read_bytes())
                key.verify(sig, raw)
                data = json.loads(raw)
                if data['protocol'] != 1 or data['nonce'] != nonce or data['device'] != device:
                    raise ValueError('响应与请求或设备不匹配')
                if data['status'] != 'active':
                    self.deadline = 0
                    reasons = {'bound': '授权码已绑定其他设备，请联系管理员解绑后换机', 'revoked': '设备绑定已撤销，请联系管理员', 'disabled': '授权已禁用，请联系管理员', 'expired': '授权已到期，请联系管理员续期', 'device_mismatch': '设备与授权绑定不匹配', 'invalid': '授权码无效，请检查后重试', 'unbound': '设备已解绑，请重新激活'}
                    self.data.update(status=data['status'], reason=reasons.get(data['status'], '授权校验未通过，请联系管理员'))
                else:
                    server, expiry, until = (data[k] for k in ('server_time','expires_at','valid_until'))
                    if any(type(v) not in (int,float) for v in (server,expiry,until)) or not server < until <= min(expiry, server+1800) or type(data['binding_version']) is not int or not data['license_id'] or not data['credential']:
                        raise ValueError('授权响应范围无效')
                    if code is None and any(data[k] != saved.get(k) for k in ('license_id', 'binding_version', 'credential')):
                        raise ValueError('绑定凭证不匹配')
                    self.write(KEY, json.dumps({k: data[k] for k in ('license_id', 'binding_version', 'credential')}))
                    self.needs_resume = False
                    self.authorized_device = device
                    self.deadline = start + until-server
                    self.next_check = start + 600
                    self.data.update(status='active', reason='', expires_at=datetime.fromtimestamp(expiry, timezone.utc).isoformat())
            except Exception as exc:
                self.data.update(status='offline', reason='联网或签名校验失败，请重试')
            return self.status()

    def allowed(self):
        state = self.status()
        if self.needs_resume:
            state = self.validate()
        return state['valid']

    def start(self):
        self.deadline = 0
        self.validate()
        self.event.clear()
        def run():
            while not self.event.wait(1):
                with self.lock:
                    due = self._time() >= self.next_check
                if due:
                    self.validate()
                    with self.lock:
                        self.next_check = self._time() + 600
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=run, daemon=True, name='license-refresh')
            self.thread.start()

    def stop(self):
        self.event.set()
        if self.thread:
            self.thread.join(timeout=12)


_client = LicenseClient()
def status(): return _client.status()
def revalidate(): return _client.validate()
def activate(code):
    result = _client.validate(code.strip())
    if not result['valid']: raise ValueError(result['reason'])
    return result
def start(): _client.start()
def stop(): _client.stop()
def is_active(): return _client.allowed()
class LicenseRequired(PermissionError): pass
def require_active():
    if not is_active(): raise LicenseRequired(status()['reason'] or '授权无效，请重新校验')
