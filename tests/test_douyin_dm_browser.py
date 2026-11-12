"""Real browser UI with isolated demo accounts and intercepted DM API only."""
import os
from pathlib import Path
import pytest
from patchright.sync_api import expect
from test_web_appearance_browser import ui, navigate, capture

pytestmark = [pytest.mark.local_cdp, pytest.mark.skipif(os.environ.get('CREATORHUB_RUN_LOCAL_CDP') != '1', reason='opt-in isolated browser')]

def test_workbench_douyin_dm_single_and_batch(ui):
    page, errors = ui
    web = Path(__file__).resolve().parents[1] / "app/web"
    page.add_script_tag(path=str(web / "submissions.js"))
    page.add_script_tag(path=str(web / "douyin-dm.js"))
    page.add_script_tag(content="""(() => {
      window.dmFixture = {posts:[], cancel:0};
      const original = window.fetch;
      window.fetch = async (url, options={}) => {
        const path = new URL(String(url), location.origin).pathname + new URL(String(url), location.origin).search;
        const response = value => Promise.resolve(new Response(JSON.stringify(value), {headers:{'Content-Type':'application/json'}}));
        if(path.startsWith('/api/dm/conversations?')) return response([{id:1,conv_id:'existing',peer_uid:'11',peer_nickname:'已有联系人'}]);
        if(path === '/api/douyin/dm/preview') {
          const body=JSON.parse(options.body), recipients=[], invalid=[];
          if(body.contact_ids.length) recipients.push({target_uid:'11',target_sec_uid:'sec11',target_nick:'已有联系人',conv_id:'existing'});
          for(const link of body.links) {
            if(!link.startsWith('https://www.douyin.com/user/')) invalid.push({input:link,error:'无效主页链接'});
            else recipients.push({target_uid:'22',target_sec_uid:'sec22',target_nick:'新联系人',conv_id:''});
          }
          const unique = [...new Map(recipients.map(r=>[r.target_uid,r])).values()];
          return response({recipients:unique,invalid,duplicates:recipients.length-unique.length});
        }
        const result = {batch_id:'fixture-batch', total:2, counts:{pending:dmFixture.cancel?0:1,done:1,canceled:dmFixture.cancel?1:0},tasks:[{target_nick:'已有联系人',status:'done'},{target_nick:'新联系人',status:dmFixture.cancel?'canceled':'pending'}]};
        if(path === '/api/douyin/dm/batches' && options.method==='POST') {dmFixture.posts.push({body:JSON.parse(options.body),key:new Headers(options.headers).get('Idempotency-Key')}); document.documentElement.dataset.dmPosts=JSON.stringify(dmFixture.posts); return response(result);}
        if(path.endsWith('/fixture-batch/cancel')) {dmFixture.cancel++;return response(result);}
        if(path.startsWith('/api/douyin/dm/batches')) return response(result);
        return original(url, options);
      };
    })();""")
    navigate(page, 'hub')
    page.locator('[data-hubtab=dm]').click()
    page.get_by_role('button', name='新建私信', exact=True).click()
    dialog=page.locator('#douyin-dm-dialog')
    page.get_by_role('button',name='解析并预览',exact=True).click()
    expect(page.locator('#dy-dm-error')).to_have_text('请填写文字内容')
    page.locator('#dy-dm-links').fill('https://www.douyin.com/user/sec22')
    page.locator('#dy-dm-content').fill('模拟单发')
    page.get_by_role('button',name='解析并预览',exact=True).click()
    expect(page.locator('#dy-dm-preview')).to_contain_text('新联系人')
    page.get_by_role('button',name='确认创建发送任务',exact=True).click()
    expect(dialog).to_contain_text('总数 2')
    dialog.get_by_role('button',name='关闭',exact=True).click()
    page.get_by_role('button',name='批量私信',exact=True).click()
    page.locator('[name="dy-dm-contact"]').check()
    page.locator('#dy-dm-links').fill('https://www.douyin.com/user/sec22\nhttps://www.douyin.com/user/sec22\ninvalid')
    page.locator('#dy-dm-content').fill('模拟批量')
    page.get_by_role('button',name='解析并预览',exact=True).click()
    expect(page.locator('#dy-dm-preview')).to_contain_text('有效 2 人 · 重复 1 项 · 无效 1 项')
    capture(page,'douyin-dm-batch-preview')
    page.get_by_role('button',name='确认创建发送任务',exact=True).dblclick()
    expect(dialog).to_contain_text('成功 1')
    page.get_by_role('button',name='取消尚未执行',exact=True).click()
    expect(dialog).to_contain_text('已取消 1')
    capture(page,'douyin-dm-batch-results')
    posted=page.locator('html').get_attribute('data-dm-posts')
    import json
    posted=json.loads(posted)
    assert len(posted)==2
    assert all(p['key'] for p in posted)
    assert posted[0]['body']['content']=='模拟单发'
    assert len(posted[1]['body']['recipients'])==2
    assert errors==[]


# These platform contract tests exercise an authorized running client.
# License-denial behavior is covered by test_device_business_gate.py.
import pytest as _license_pytest

@_license_pytest.fixture(autouse=True)
def _authorized_platform_test(monkeypatch):
    monkeypatch.setattr("app.licensing.is_active", lambda: True)
