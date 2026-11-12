import pytest
from app import licensing

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
async def test_api_denial_preserves_export(license_db, monkeypatch):
    import httpx
    from app import main
    active = [False]
    monkeypatch.setattr(licensing, 'is_active', lambda: active[0])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, client=('127.0.0.1', 123)), base_url='http://127.0.0.1') as client:
        assert (await client.post('/api/monitors', json={})).status_code == 403
        assert (await client.get('/api/reports/monitors.xlsx')).status_code == 200
        active[0] = True
        assert (await client.post('/api/monitors', json={})).status_code == 422
        active[0] = False
        assert (await client.post('/api/monitors', json={})).status_code == 403
        assert (await client.get('/api/reports/monitors.xlsx')).status_code == 200

@pytest.mark.asyncio
async def test_webhook_batch_stops_before_next_event(license_db, monkeypatch):
    from app import event_center, db
    from app.models import WebhookEvent, WebhookSubscription
    import httpx
    clock = [150]
    monkeypatch.setattr(licensing, 'is_active', lambda: clock[0] < 200)
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
    clock[0] = 150
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
