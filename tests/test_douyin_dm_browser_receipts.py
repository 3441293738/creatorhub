"""Independent offline checks of the browser sender; never opens a browser."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.browser import account_hub as hub

class Capture:
    def __init__(self, response): self.response=response
    async def __aenter__(self):
        async def result(): return self.response
        self.value=result()
        return self
    async def __aexit__(self, typ, value, tb):
        if typ: self.value.close()
        return False

@pytest.mark.parametrize('raw,status,click_error,expected', [
    (b'{"cmd":100,"status_code":0}',200,False,'done'),
    (b'{"cmd":100,"status_code":8,"error_desc":"rejected"}',200,False,'failed'),
    (b'{"cmd":2043,"status_code":0}',200,False,'uncertain'),
    (b'<html>gateway</html>',502,False,'uncertain'),
    (b'{"cmd":100,"status_code":0}',200,True,'uncertain'),
])
def test_browser_send_has_platform_receipt_and_no_second_submit(raw,status,click_error,expected):
    def locator():
        obj=SimpleNamespace(count=AsyncMock(return_value=1),is_visible=AsyncMock(return_value=True),
                            is_enabled=AsyncMock(return_value=True),click=AsyncMock())
        obj.first=obj
        return obj
    entry,editor,button=locator(),locator(),locator()
    if click_error: button.click.side_effect=TimeoutError('connection lost after click')
    response=SimpleNamespace(url=hub._SEND_URL,request=SimpleNamespace(method='POST'),status=status,body=AsyncMock(return_value=raw))
    page=SimpleNamespace(url='https://www.douyin.com/user/test',goto=AsyncMock(),wait_for_timeout=AsyncMock(),
        get_by_text=lambda *a,**k:entry,locator=lambda sel:editor if sel in hub._DM_INPUT else button,
        keyboard=SimpleNamespace(type=AsyncMock(),press=AsyncMock()),close=AsyncMock())
    def expect(predicate,**kwargs):
        assert predicate(response)
        assert not predicate(SimpleNamespace(url=hub._SEND_URL,request=SimpleNamespace(method='GET')))
        return Capture(response)
    page.expect_response=expect
    ctx=SimpleNamespace(new_page=AsyncMock(return_value=page),close=AsyncMock())
    mgr=SimpleNamespace(open_headed=AsyncMock(return_value=ctx))
    ok,error=asyncio.run(hub.send_dm(mgr,None,'douyin',target_sec_uid='fixture',text='test'))
    actual='done' if ok else 'uncertain' if error.startswith('write_uncertain:') else 'failed'
    assert actual==expected
    assert button.click.await_count==1
    page.keyboard.press.assert_not_awaited()
    page.close.assert_awaited_once()
    ctx.close.assert_awaited_once()
