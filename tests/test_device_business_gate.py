import pytest
from app import licensing

@pytest.mark.asyncio
async def test_queued_expiry_and_inflight_completion(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from app.engine.monitor import MonitorEngine
    from app.risk import OperationKind
    clock = [150]
    monkeypatch.setattr(licensing, 'is_active', lambda: clock[0] < 200)
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
    clock[0] = 150
    await operation('renewed')
    assert calls == ['first', 'first-settled', 'renewed', 'renewed-settled']


def test_expired_pre_submit_does_not_mark_task(monkeypatch):
    from app.engine.monitor import MonitorEngine
    monkeypatch.setattr(licensing, "is_active", lambda: False)
    with pytest.raises(licensing.LicenseRequired):
        MonitorEngine._mark_write_submit(None, 1)

@pytest.mark.asyncio
async def test_direct_submit_helpers_reject_before_callback(monkeypatch):
    from app.browser.xhs_dm import _mark_submit as xhs
    from app.browser.tiktok_actions import _mark_submit as tiktok
    from app.platforms.tiktok.publish import _notify_submit as publish
    from app.platforms.xhs.browser_writes import _SubmitLocator
    monkeypatch.setattr(licensing, 'is_active', lambda: False)
    calls = []
    for helper in (xhs, tiktok, publish):
        with pytest.raises(licensing.LicenseRequired):
            await helper(lambda: calls.append('submitted'))
    with pytest.raises(licensing.LicenseRequired):
        await _SubmitLocator(None, {}, lambda: calls.append('submitted')).click()
    assert calls == []
