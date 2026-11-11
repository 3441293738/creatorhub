import base64
import json
import pytest
from types import SimpleNamespace
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from app import licensing

@pytest.fixture
def issuer(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    public = tmp_path / 'public.pem'
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setattr(licensing, 'PUBLIC_KEY', public)
    def issue(expires=200, issued=100):
        raw = json.dumps(dict(product='huoke-assistant-v1', customer='test', issued=issued, expires=expires)).encode()
        return base64.urlsafe_b64encode(raw).decode() + '.' + base64.urlsafe_b64encode(key.sign(raw)).decode()
    return issue

def test_valid_expiry_and_future(issuer):
    code = issuer()
    assert licensing.status(code, 150)['valid']
    assert licensing.status(code, 200)['status'] == 'expired'
    assert licensing.status(code, 99)['status'] == 'invalid'

def test_tampered_and_wrong_codes(issuer):
    code = issuer()
    payload, signature = code.split('.')
    raw = base64.urlsafe_b64decode(payload).replace(b'test', b'evil')
    tampered = base64.urlsafe_b64encode(raw).decode() + '.' + signature
    for value in ('garbage', tampered, 'a.b', issuer(90)):
        assert licensing.status(value, 150)['status'] == 'invalid'

def test_activation_preserves_existing_on_error_and_renews(issuer, monkeypatch):
    store = {}
    monkeypatch.setattr(licensing, 'get_setting', lambda key: store.get(key, ''))
    monkeypatch.setattr(licensing, 'set_setting', lambda key, value: store.update({key:value}))
    monkeypatch.setattr(licensing, 'time', SimpleNamespace(time=lambda: 150))
    assert licensing.status()['status'] == 'inactive'
    licensing.activate(issuer())
    assert licensing.status()['valid']
    with pytest.raises(ValueError):
        licensing.activate('broken')
    assert licensing.status()['valid']
    monkeypatch.setattr(licensing, 'time', SimpleNamespace(time=lambda: 201))
    assert licensing.status()['status'] == 'expired'
    licensing.activate(issuer(300))
    assert licensing.status()['valid']

@pytest.fixture
def license_db(tmp_path, monkeypatch):
    from app import db
    previous = db._engine
    path = str(tmp_path / 'license.db')
    db.init_db(path)
    yield path
    db._engine.dispose()
    db._engine = previous

@pytest.mark.asyncio
async def test_api_activation_export_and_real_db_reopen(issuer, license_db, monkeypatch):
    import httpx
    from app import main, db
    monkeypatch.setattr(licensing, 'time', SimpleNamespace(time=lambda: 150))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, client=('127.0.0.1', 123)), base_url='http://127.0.0.1') as client:
        assert (await client.post('/api/monitors', json={})).status_code == 403
        assert (await client.get('/api/reports/monitors.xlsx')).status_code == 200
        assert (await client.post('/api/license/activate', json={'code':'bad'})).status_code == 400
        assert (await client.post('/api/license/activate', json={'code':issuer()})).status_code == 200
        db._engine.dispose()
        db.init_db(license_db)
        assert (await client.get('/api/license')).json()['valid']
        # Invalid request reaches business validation rather than licensing gate.
        assert (await client.post('/api/monitors', json={})).status_code == 422
        monkeypatch.setattr(licensing, 'time', SimpleNamespace(time=lambda: 201))
        assert (await client.post('/api/monitors', json={})).status_code == 403
        assert (await client.get('/api/reports/monitors.xlsx')).status_code == 200
        assert (await client.post('/api/license/activate', json={'code':issuer(300)})).status_code == 200
        assert (await client.post('/api/monitors', json={})).status_code == 422

@pytest.mark.asyncio
async def test_queued_expiry_and_inflight_completion(issuer, license_db, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from app.engine.monitor import MonitorEngine
    from app.risk import OperationKind
    clock = [150]
    monkeypatch.setattr(licensing, 'time', SimpleNamespace(time=lambda: clock[0]))
    licensing.activate(issuer())
    lock = asyncio.Lock()
    @asynccontextmanager
    async def network_guard(account):
        yield
    engine = object.__new__(MonitorEngine)
    engine.browser = SimpleNamespace(lock_for=lambda key: lock)
    engine.risk = SimpleNamespace(network_guard=network_guard)
    engine._active_sem = asyncio.Semaphore(2)
    calls = []
    entered, finish = asyncio.Event(), asyncio.Event()
    async def operation(label, wait=False):
        async with engine._operation_guard(None, OperationKind.PUBLISH):
            calls.append(label)
            if wait:
                entered.set()
                await finish.wait()
            calls.append(label + '-settled')
    first = asyncio.create_task(operation('first', True))
    await entered.wait()
    queued = asyncio.create_task(operation('queued'))
    await asyncio.sleep(0)
    clock[0] = 201
    finish.set()
    await first
    with pytest.raises(licensing.LicenseRequired):
        await queued
    assert calls == ['first', 'first-settled']
    licensing.activate(issuer(300))
    await operation('renewed')
    assert calls == ['first', 'first-settled', 'renewed', 'renewed-settled']

@pytest.mark.asyncio
async def test_webhook_batch_stops_before_next_event(issuer, license_db, monkeypatch):
    from app import event_center, db
    from app.models import WebhookEvent, WebhookSubscription
    import httpx
    clock = [150]
    monkeypatch.setattr(licensing, 'time', SimpleNamespace(time=lambda: clock[0]))
    licensing.activate(issuer())
    with db.get_session() as session:
        session.add(WebhookSubscription(name='mock', url='https://example.invalid', event_types='["*"]'))
        session.commit()
    first = event_center.emit_event('test', {})
    second = event_center.emit_event('test', {})
    calls=[]
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            calls.append(url)
            clock[0] = 201
            return httpx.Response(200, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    await event_center.dispatch_pending()
    with db.get_session() as session:
        assert session.get(WebhookEvent, first).status == 'delivered'
        assert session.get(WebhookEvent, second).status == 'pending'
        assert session.get(WebhookEvent, second).attempts == 0
    assert len(calls) == 1
    licensing.activate(issuer(300))
    await event_center.dispatch_pending()
    assert len(calls) == 2

@pytest.mark.asyncio
async def test_receiver_resumes_after_renewal(monkeypatch):
    import asyncio
    from app.engine.im_receiver import ImReceiverManager
    active = [True]
    monkeypatch.setattr(licensing, 'is_active', lambda: active[0])
    manager = ImReceiverManager(None)
    manager._accts[1] = {'queues': {object()}}
    renewed = asyncio.Event()
    calls=[]
    async def receive(account_id):
        calls.append(account_id)
        if len(calls) == 1:
            active[0] = False
            asyncio.get_running_loop().call_later(0.02, lambda: active.__setitem__(0, True))
        else:
            renewed.set()
    monkeypatch.setattr(manager, '_receive_once', receive)
    task = asyncio.create_task(manager._run(1))
    await asyncio.wait_for(renewed.wait(), 2)
    await task
    assert calls == [1, 1]
