"""视频号发布入口(浏览器自动化视频号助手 channels.weixin.qq.com)。

视频号发布页是 wujie 微前端(内容在 shadowRoot 里)。Playwright 的定位器默认会穿透
**开放的** shadow DOM,故下面用普通 CSS 选择器即可;若视频号把 shadow root 设成 closed
则需改用 CDP pierce(参见 _CHANNELS_* 注释)。

⚠️ 实验性 + 需校准:发布页选择器随视频号改版/wujie 版本变化,集中在下面 _* 常量;
   选择器初值取自对小V猫的逆向观察(`.post-view` 系),务必用真实账号发一条核对。
   发布时弹真实窗口,遇「实名/过脸验证」「封面必填」可在窗口里手动处理。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Tuple

from ...browser.identity import Identity
from ...browser.manager import BrowserManager

log = logging.getLogger("creatorhub.channels")

# 视频号发布入口(实测):就是 create 页;视频/图文靠点左侧「视频」「图文」导航切换
# (逆向 server.jsc 里还有 finderNewLifeCreateg,但实测会 302 回 create,故不直接跳它)。
CREATE_URL_VIDEO = "https://channels.weixin.qq.com/platform/post/create"
CREATE_URL = CREATE_URL_VIDEO   # 兼容旧引用

# 标题/描述编辑器(wujie shadowRoot 内)。视频号「短标题」与「描述」是两个框,
# 这里优先填描述编辑器;短标题选择器留作备用。
_DESC_SEL = [
    '.post-view .post-desc-box .input-editor',
    '.post-desc-box .input-editor',
    'div.input-editor[contenteditable="true"]',
    'textarea[placeholder*="描述"]',
    'div[contenteditable="true"]',
]
_SHORT_TITLE_SEL = [
    '.post-view .short-title-wrap input',
    'input[placeholder*="标题"]',
]
# 文件上传 input(视频号发布页可能在 wujie iframe / shadowRoot 里)
_FILE_SEL = [
    '.post-view .upload input[type="file"]',
    '.post-view input[type="file"]',
    'input[type="file"][accept*="video"]',
    'input[type="file"]',
]
# 上传区(点击会弹原生文件对话框,必须用 expect_file_chooser 拦截,不能裸点)。
# 真实 UI:虚线「+」框,内含「上传时长8小时内…」提示文案。
_UPLOAD_ZONE = [
    'text=上传时长', 'text=上传图片', 'text=从这里上传',
    '.center-upload', '.finder-upload', '[class*="upload-entry"]',
    '[class*="upload"]',
]
# 左侧内容管理导航:视频 / 图文(点它进对应「列表页」)
_IMAGE_NAV = [
    'text=图文', 'a:has-text("图文")', 'li:has-text("图文")',
    '[class*="menu"]:has-text("图文")',
]
# 图文列表页里的「发表图文」按钮(在 micro/content iframe 内,必须跨 frame 点)
_CREATE_IMAGE_BTN = ['text=发表图文', 'button:has-text("发表图文")',
                     '[class*="btn"]:has-text("发表图文")']
# 发布按钮
_PUBLISH_BTN = [
    '.post-view .form-btns button.weui-desktop-btn_primary',
    '.post-view .form-btns button',
    'button:has-text("发表")',
    'button:has-text("发布")',
]
# 发布成功判据(页面跳转/出现提示)
_OK_TEXTS = ["发表成功", "发布成功", "提交成功"]

# ── 位置 POI(可选;选择器需真号校准,任何一步失败都跳过、不阻塞发布)──
_POI_TRIGGER = [
    'text=不显示位置', '[class*="position"]', '[class*="location"]',
    '.location-display', '.position-select',
]
_POI_INPUT = [
    'input[placeholder*="搜索位置"]', 'input[placeholder*="搜索地点"]',
    'input[placeholder*="位置"]', '[class*="position"] input',
    '[class*="location"] input',
]
_POI_RESULT = [
    '[class*="position"] [class*="item"]', '[class*="location"] [class*="item"]',
    '[class*="poi"] [class*="item"]', '[class*="position"] li',
    '.dropdown-item', '[class*="option"]',
]


async def _fill_first(page, selectors, text, timeout=2500) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.click(timeout=timeout)
            try:
                await el.fill(text, timeout=timeout)
            except Exception:
                await page.keyboard.type(text, delay=30)   # contenteditable 不支持 fill
            return True
        except Exception:
            continue
    return False


async def _click_first(page, selectors, timeout=3000) -> bool:
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.count():
                await btn.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


async def _click_in_frames(page, selectors, timeout=4000) -> bool:
    """在主页面 + 所有子 frame(micro/content iframe)里点第一个命中的元素。
    视频号发布 UI 在 iframe 里,普通 page.click 点不到,必须遍历 frames。"""
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if await loc.count():
                    await loc.click(timeout=timeout)
                    return True
            except Exception:
                continue
    return False


async def _set_location(page, location: str):
    """设置视频号位置 POI(可选,best-effort)。任何一步失败都只记日志、跳过,不影响发布。
    流程:点开位置控件 -> 输入搜索 -> 点第一个结果。选择器需真号校准(见 _POI_*)。"""
    if not location:
        return
    try:
        if not await _click_in_frames(page, _POI_TRIGGER, timeout=3000):
            log.warning("[channels_publish] 未找到位置控件,跳过位置设置")
            return
        await page.wait_for_timeout(1200)
        inp, _fr = await _find_in_frames(page, _POI_INPUT)
        if inp is None:
            log.warning("[channels_publish] 未找到位置搜索框,跳过位置(位置选择器需校准 _POI_INPUT)")
            return
        await inp.click()
        await page.keyboard.type(location, delay=40)
        await page.wait_for_timeout(2200)      # 等搜索结果回来
        if not await _click_in_frames(page, _POI_RESULT, timeout=3000):
            log.warning("[channels_publish] 位置「%s」无匹配结果或结果选择器需校准 _POI_RESULT", location)
        else:
            log.info("[channels_publish] 已设置位置: %s", location)
    except Exception as e:
        log.warning("[channels_publish] 设置位置异常(已跳过): %r", e)


async def _find_in_frames(page, selectors):
    """在主页面 + 所有子 frame(wujie iframe)里找第一个命中的定位器。
    Playwright 定位器默认穿透**开放** shadowRoot,但不穿 iframe,故需遍历 page.frames。
    返回 (locator, frame) 或 (None, None)。"""
    for frame in page.frames:                # page.frames[0] 即主 frame
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if await loc.count():
                    return loc, frame
            except Exception:
                continue
    return None, None


async def _collect_diag(page, tag: str) -> str:
    """采集发布页真实 DOM 诊断,返回**紧凑单行摘要**(会拼进 UI 错误文案,你不用翻控制台),
    同时打到服务端日志。每个 frame 报:普通/shadow 里的 file input 数、upload 元素数、
    .post-view 数、按钮文案。"""
    parts = []
    try:
        for i, fr in enumerate(page.frames):
            try:
                info = await fr.evaluate("""() => {
                    const q = (s) => document.querySelectorAll(s).length;
                    let sf = 0;
                    const walk = (root) => root.querySelectorAll('*').forEach(el => {
                        if (el.shadowRoot) { sf += el.shadowRoot.querySelectorAll('input[type=file]').length; walk(el.shadowRoot); }
                    });
                    try { walk(document); } catch(e){}
                    const btns = [...document.querySelectorAll('button,[role=button],.weui-desktop-btn')]
                        .map(b => (b.innerText||'').trim()).filter(Boolean).slice(0, 8);
                    const host = (location.host||'') + (location.pathname||'');
                    return `${host} fi=${q('input[type=file]')} sf=${sf} up=${q('[class*=upload]')} pv=${q('.post-view')} btn=[${btns.join('/')}]`;
                }""")
                parts.append(f"f{i}:{info}")
            except Exception:
                parts.append(f"f{i}:eval_err")
    except Exception:
        pass
    summary = " | ".join(parts) or "无 frame 信息"
    log.warning("[channels_publish/%s] %s", tag, summary)
    return summary


async def publish_channels(mgr: BrowserManager, identity: Identity,
                           storage_state_json: str, media_type: str, title: str,
                           desc: str, media_paths: List[str], topics: str = "",
                           headed: bool = True, timeout_seconds: int = 180,
                           location: str = ""
                           ) -> Tuple[bool, str, str]:
    """发布一条视频号作品。返回 (ok, result_url, error)。
    location:可选,视频号位置 POI(best-effort,设不上不影响发布)。
    storage_state_json 仅校验用,实际登录态在该账号持久 profile 里。"""
    files = [str(Path(p)) for p in media_paths if p and Path(p).exists()]
    if not files:
        return False, "", "没有可用的本地媒体文件(路径不存在)"
    tags = [t.strip().lstrip("#") for t in (topics or "").split(",") if t.strip()]
    # 视频号正文:描述 + 话题(标题另填短标题框)
    body = ((desc or "")
            + ("\n" + " ".join(f"#{t}" for t in tags) if tags else "")).strip()[:1000]

    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    ok, result_url, error = False, "", ""
    try:
        # 视频号发布入口就是 create 页;图文/视频靠点左侧导航切换(finderNewLifeCreateg 会 302 回 create)
        await page.goto(CREATE_URL_VIDEO, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(4000)
        if "login.html" in page.url or page.url.rstrip("/").endswith("/login"):
            return False, "", f"logged_out:视频号助手未登录(落到 {page.url})"

        # 图文:点左侧「图文」→ 图文列表页 → 点「发表图文」→ 图文发布表单
        # (视频号 UI 在 micro/content iframe 里,按钮要跨 frame 点)
        if media_type == "images":
            await _click_in_frames(page, _IMAGE_NAV, timeout=4000)
            await page.wait_for_timeout(2000)
            if await _click_in_frames(page, _CREATE_IMAGE_BTN, timeout=5000):
                await page.wait_for_timeout(2500)
            else:
                diag = await _collect_diag(page, "no-create-image-btn")
                return False, "", f"未找到「发表图文」按钮,无法进图文发布页。DOM诊断: {diag}"

        want = files if media_type == "images" else files[:1]
        uploaded = False
        # 首选:直接给隐藏 <input type=file> 塞文件(不弹原生对话框,最稳)
        up, _fr = await _find_in_frames(page, _FILE_SEL)
        if up is not None:
            try:
                await up.set_input_files(want, timeout=20000)
                uploaded = True
            except Exception:
                uploaded = False
        # 兜底:input 是点击时按需创建 —— 用 expect_file_chooser 拦截原生文件框(关键!
        # 裸点上传区会弹 Windows「打开」对话框把 Playwright 卡死,必须这样接管)
        if not uploaded:
            try:
                async with page.expect_file_chooser(timeout=10000) as fc_info:
                    if not await _click_in_frames(page, _UPLOAD_ZONE, timeout=4000):
                        raise RuntimeError("未点到上传区")
                chooser = await fc_info.value
                await chooser.set_files(want)
                uploaded = True
            except Exception:
                uploaded = False
        if not uploaded:
            diag = await _collect_diag(page, "upload-failed")
            return False, "", (f"上传失败(未找到可用上传入口/文件选择器)。DOM诊断: {diag}")

        # 等待转码/上传(视频较久)
        await page.wait_for_timeout(8000 if media_type == "video" else 4000)

        if title:
            el, _tf = await _find_in_frames(page, _SHORT_TITLE_SEL)
            if el is not None:
                try:
                    await el.fill(title[:16])
                except Exception:
                    pass
        if body:
            el, _df = await _find_in_frames(page, _DESC_SEL)
            if el is not None:
                try:
                    await el.click()
                    await page.keyboard.type(body, delay=20)
                except Exception:
                    pass
        # 位置 POI(可选,best-effort)
        await _set_location(page, location)
        await page.wait_for_timeout(1000)

        pub, _pf = await _find_in_frames(page, _PUBLISH_BTN)
        if pub is None:
            diag = await _collect_diag(page, "no-publish-btn")
            return False, "", (f"上传/填写已完成但未找到发表按钮。DOM诊断: {diag}")
        try:
            await pub.click(timeout=5000)
        except Exception as e:
            return False, "", f"点发表失败: {e!r}"

        # 等成功:视频号发表后会**跳到「图文/视频管理」列表页**(URL 含 PostList),
        # 或短暂弹「发表成功」toast。以跳列表页为主判据(实测 finderNewLifePostList)。
        for _ in range(int(timeout_seconds / 2)):
            url_l = page.url.lower()
            # finderNewLifePostList / post/list 等管理列表页 -> 发表成功后的落点
            if "postlist" in url_l or "/post/list" in url_l:
                ok = True
                break
            # toast 可能在主页面或 micro/content iframe 里
            for fr in page.frames:
                try:
                    if any([await fr.get_by_text(t, exact=False).count() for t in _OK_TEXTS]):
                        ok = True
                        break
                except Exception:
                    pass
            if ok:
                break
            await page.wait_for_timeout(2000)
        result_url = page.url if ok else ""
        if not ok:
            diag = await _collect_diag(page, "no-success")
            error = ("已点发表但未确认成功(视频号可能要求封面/实名/过脸验证,请到助手确认)。"
                     f"当前页: {page.url}; DOM诊断: {diag}")
    except Exception as e:
        error = f"发布异常: {e!r}"
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return ok, result_url, error
