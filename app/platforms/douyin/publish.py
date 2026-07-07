"""抖音发布入口(浏览器自动化抖音创作者服务平台 creator.douyin.com)。

抖音 PC 端上传要分片传 upload 节点 + a_bogus 签名 + create_aweme,纯 HTTP 直发链路长、
易随改版失效;发布是低频写操作,和快手一样走浏览器自动化性价比最高,也贴合本项目
「登录态浏览器 + 免手写签名」的路线:用账号专属持久 profile(已含创作者登录态)打开
发布页,上传文件、填标题/正文、点发布。

⚠️ 实验性:发布页选择器随抖音改版可能失效,集中在下面的 _* 选择器常量;
   发布时弹真实窗口,遇滑块验证 / 需补封面 / 定位话题必填可在窗口里手动处理。

调试:失败(含「已点发布但未确认成功」)时会把截图 + 页面 URL/文本快照写到
   data/debug/dy_publish_*.{png,txt},并在服务端控制台打印 [dy-publish] 日志。
   抖音创作平台改版频繁,首次真机发布基本都要据此把选择器校准一次。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

from ...browser.identity import Identity
from ...browser.manager import BrowserManager

UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
# 图文发布入口(default-tab=3 直达「发布图文」;失败再退回点 tab)
IMAGE_URL = "https://creator.douyin.com/creator-micro/content/upload?default-tab=3"
_TAB_IMAGE = ['div:has-text("发布图文")', 'text=发布图文', 'text=图文']
# 视频有独立「标题」短标题输入;图文有「作品标题」
_TITLE_SEL = ['input[placeholder*="填写作品标题"]', 'input[placeholder*="作品标题"]',
              'input[placeholder*="标题"]', '.title-input input', 'input.semi-input']
# 正文/简介富文本(抖音创作者用 editor-kit 富文本,contenteditable)
_DESC_SEL = ['.editor-kit-editor-container [contenteditable="true"]',
             'div[data-placeholder*="简介"]', 'div[data-placeholder*="作品"]',
             'div[contenteditable="true"]', '.zone-container',
             'textarea[placeholder*="简介"]', 'textarea']
# 最终提交按钮(排除「定时发布」等干扰,后面再按 enabled + 文本精确过滤)
_PUBLISH_BTN = ['button:has-text("发布")', 'button:has-text("发布作品")',
                'div[class*="content-confirm"] button', '.publish-btn button', '.button-publish']

_DEBUG_DIR = Path("./data/debug")

# 发布成功信号(文案任一命中即算成功)
_SUCCESS_KW = ("发布成功", "作品发布成功", "投稿成功", "发布完成", "已发布", "审核中")
# 抖音风控人工验证(短信验证码 / 扫码 / 滑块):需用户在弹出窗口里手动完成,不能自动化
_VERIFY_KW = ("接收短信验证码", "短信验证码", "为确保是本人操作", "输入验证码",
              "安全验证", "完成验证", "拖动滑块", "使用原设备扫码", "身份验证")


def _log(msg: str) -> None:
    print(f"[dy-publish] {msg}", flush=True)


async def _visible_any(page, keywords) -> str:
    """返回第一个当前可见的关键词文案(无则空串);用于快速轮询,不阻塞等待。"""
    for kw in keywords:
        try:
            if await page.get_by_text(kw, exact=False).first.is_visible():
                return kw
        except Exception:
            continue
    return ""


async def _dump(page, tag: str) -> str:
    """把当前页面截图 + URL/可见文本快照落盘,返回截图路径(供日志/前端提示)。"""
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = _DEBUG_DIR / f"dy_publish_{tag}_{stamp}"
        png = str(base.with_suffix(".png"))
        try:
            await page.screenshot(path=png, full_page=True)
        except Exception:
            png = ""
        try:
            txt = await page.inner_text("body")
        except Exception:
            txt = ""
        base.with_suffix(".txt").write_text(
            f"url: {page.url}\n\n{txt[:4000]}", encoding="utf-8")
        _log(f"已存诊断快照 tag={tag} url={page.url} png={png}")
        return png
    except Exception as e:
        _log(f"存快照失败: {e!r}")
        return ""


async def _click_first(page, selectors, timeout=2500) -> bool:
    for sel in selectors:
        try:
            await page.click(sel, timeout=timeout)
            return True
        except Exception:
            continue
    return False


async def _fill_first(page, selectors, text, timeout=3000) -> bool:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="visible", timeout=timeout)
            await el.click(timeout=timeout)
            try:
                await el.fill(text, timeout=timeout)
            except Exception:
                # contenteditable 富文本 fill 不一定支持,退回键入
                await page.keyboard.type(text)
            _log(f"填入文本用选择器 {sel!r}")
            return True
        except Exception:
            continue
    return False


async def _wait_editor(page, timeout_ms: int) -> bool:
    """等发布表单出现(简介编辑器可见)= 上传/转码完成、已跳到编辑页。"""
    for sel in _DESC_SEL:
        try:
            await page.locator(sel).first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


async def _primary_publish_button(page):
    """挑最终「发布」按钮:文本恰为发布/发布作品、可见、可用;取最后一个(表单底部)。"""
    try:
        btns = page.locator('button')
        n = await btns.count()
    except Exception:
        return None
    cand = None
    for i in range(n):
        b = btns.nth(i)
        try:
            if not await b.is_visible():
                continue
            t = ((await b.inner_text()) or "").strip()
            if t in ("发布", "发布作品", "发布视频", "发布图文"):
                cand = b   # 页面里可能多个,取靠后的(底部提交)
        except Exception:
            continue
    return cand


async def _dump_radios(page) -> None:
    """把页面上疑似单选项/可点标签的真实文案打出来,便于按实际文案校准。"""
    try:
        texts = await page.eval_on_selector_all(
            'label, [role="radio"], .semi-radio, .semi-radio-addon, [class*="radio"]',
            "els => els.map(e => (e.innerText||'').trim()).filter(t => t && t.length <= 12)")
        seen, out = set(), []
        for t in texts:
            if t not in seen:
                seen.add(t); out.append(t)
        _log(f"页面可选项文案候选: {out[:40]}")
    except Exception as e:
        _log(f"枚举单选项失败: {e!r}")


async def _choose_radio(page, label: str) -> bool:
    """点一个单选项。**只走安全精确策略**,绝不 force / 不点任意祖先 ——
    否则可能误点「继续添加/封面」等触发系统选图框的按钮,卡死自动化。"""
    # 1) 无障碍 role=radio(Semi 给单选组件挂 role),用 check() 语义最稳
    try:
        loc = page.get_by_role("radio", name=label, exact=True)
        if await loc.count():
            await loc.first.scroll_into_view_if_needed(timeout=2000)
            try:
                await loc.first.check(timeout=2500)
            except Exception:
                await loc.first.click(timeout=2500)
            _log(f"已选发布设置「{label}」(role)")
            return True
    except Exception:
        pass
    # 2) 仅在 Semi 单选组件内、且整项文本恰等于 label 的元素上点(不碰其它任何东西)
    try:
        radios = page.locator('label.semi-radio, .semi-radio, label[class*="radio"]')
        n = await radios.count()
        for i in range(n):
            r = radios.nth(i)
            try:
                txt = ((await r.inner_text()) or "").strip()
            except Exception:
                continue
            if txt == label:
                await r.scroll_into_view_if_needed(timeout=2000)
                await r.click(timeout=2000)
                _log(f"已选发布设置「{label}」(semi-radio)")
                return True
    except Exception:
        pass
    _log(f"⚠️ 未点中发布设置「{label}」(已安全跳过,不乱点)—— 打印页面实际可选项供校准:")
    await _dump_radios(page)
    return False


async def _apply_publish_settings(page, visibility: str, allow_save: bool) -> None:
    """设置「谁可以看」「保存权限」。公开 / 允许为抖音默认值,非默认才点,减少误点。"""
    vis_label = {"friends": "好友可见", "private": "仅自己可见"}.get(visibility, "")
    if vis_label:                       # public=默认,不动
        await _choose_radio(page, vis_label)
    if not allow_save:                  # 允许=默认,只在「不允许」时点
        await _choose_radio(page, "不允许")


async def publish_douyin(mgr: BrowserManager, identity: Identity,
                         storage_state_json: str, media_type: str, title: str,
                         desc: str, media_paths: List[str], topics: str = "",
                         visibility: str = "public", allow_save: bool = True,
                         headed: bool = True, timeout_seconds: int = 180
                         ) -> Tuple[bool, str, str]:
    """发布一条抖音作品。返回 (ok, result_url, error)。
    storage_state_json 仅用于校验(实际登录态在该账号持久 profile 里)。
    visibility: public 公开 | friends 好友可见 | private 仅自己可见;allow_save: 是否允许他人保存。"""
    files = [str(Path(p)) for p in media_paths if p and Path(p).exists()]
    if not files:
        return False, "", "没有可用的本地媒体文件(路径不存在)"
    tags = [t.strip().lstrip("#") for t in (topics or "").split(",") if t.strip()]
    # 抖音正文 = 简介 + 话题(话题写进正文,发布时自动识别 #)
    body = ((desc or "") + ("\n" + " ".join(f"#{t}" for t in tags) if tags else "")).strip()[:2000]

    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()

    # 保险:媒体上传我们全用 set_input_files(不触发 filechooser),故任何 filechooser
    # 事件都是意外(比如误点了「继续添加/封面」的 <input type=file>),自动取消避免卡死。
    # 注:抖音部分选图用系统级 showOpenFilePicker,此 hook 拦不到 —— 根本对策是别乱点。
    def _fc_guard(fc):
        async def _cancel():
            try:
                await fc.set_files([])
            except Exception:
                pass
        asyncio.ensure_future(_cancel())
    try:
        page.on("filechooser", _fc_guard)
    except Exception:
        pass

    ok, result_url, error = False, "", ""
    try:
        url = IMAGE_URL if media_type == "images" else UPLOAD_URL
        _log(f"打开发布页 {url} (media_type={media_type}, files={len(files)})")
        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(3000)
        if "passport" in page.url or "/login" in page.url:
            await _dump(page, "loggedout")
            return False, "", "logged_out:抖音创作平台未登录,请先在账号页点「创作者登录」"
        if media_type == "images":
            await _click_first(page, _TAB_IMAGE, timeout=2000)
            await page.wait_for_timeout(1200)
        try:
            await page.locator('input[type="file"]').first.set_input_files(
                files if media_type == "images" else files[:1], timeout=15000)
            _log("已提交文件,等待上传/转码…")
        except Exception as e:
            await _dump(page, "nofileinput")
            return False, "", f"上传文件失败(未找到文件输入框?): {e!r}"

        # 等表单编辑器出现(= 已跳到编辑页;视频转码慢,给足时间)
        edit_timeout = 120000 if media_type == "video" else 45000
        if not await _wait_editor(page, edit_timeout):
            await _dump(page, "noeditor")
            return False, "", ("上传后未进入编辑页(视频可能仍在转码或上传失败)。"
                               "请在弹出的窗口里查看,或到抖音创作平台重试。")
        _log(f"已进入编辑页 url={page.url}")
        await page.wait_for_timeout(1500)

        if title:
            await _fill_first(page, _TITLE_SEL, title.strip()[:30])
            await page.wait_for_timeout(400)
        if body:
            if not await _fill_first(page, _DESC_SEL, body):
                _log("警告:未能填入简介(选择器可能改版)")
        await page.wait_for_timeout(800)

        # 发布设置:谁可以看 / 保存权限(公开、允许为默认,仅非默认才点)
        await _apply_publish_settings(page, visibility, allow_save)
        await page.wait_for_timeout(600)

        # 轮询等「发布」按钮可用(视频未处理完时按钮 disabled),最多 ~90s
        btn = None
        for _ in range(45):
            btn = await _primary_publish_button(page)
            if btn is not None:
                try:
                    if await btn.is_enabled():
                        break
                except Exception:
                    break
            await page.wait_for_timeout(2000)
        if btn is None:
            await _dump(page, "nobtn")
            return False, "", "未找到发布按钮(发布页可能改版)。已存诊断截图到 data/debug/。"
        try:
            await btn.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            await btn.click(timeout=5000)
            _log("已点击发布按钮")
        except Exception:
            # 兜底:用文本选择器再点一次
            if not await _click_first(page, _PUBLISH_BTN, timeout=4000):
                await _dump(page, "clickfail")
                return False, "", "发布按钮点击失败(可能被弹层遮挡/需补封面)。已存诊断截图。"

        # 成功判定 + 风控验证等待:抖音点发布后常弹「短信验证码/扫码」要本人操作,
        # 这时必须把弹出的窗口留给用户手动完成,故轮询等待、给足时间(默认最多 5 分钟)。
        ok = False
        deadline = max(int(timeout_seconds), 300)   # 秒;留足人工验证时间
        waited, verify_notified = 0, False
        while waited < deadline:
            if "/content/manage" in page.url:
                ok = True
                break
            hit = await _visible_any(page, _SUCCESS_KW)
            if hit:
                ok = True
                _log(f"命中成功文案「{hit}」")
                break
            vkw = await _visible_any(page, _VERIFY_KW)
            if vkw and not verify_notified:
                verify_notified = True
                await _dump(page, "verify")
                _log(f"⚠️ 抖音要求人工验证「{vkw}」—— 请在弹出的浏览器窗口里完成"
                     f"(输入短信验证码 / 扫码 / 滑块),完成后本流程会自动继续,最多等 {deadline}s")
            await page.wait_for_timeout(2000)
            waited += 2
        result_url = page.url if ok else ""
        if ok:
            _log(f"发布成功 url={result_url}")
        elif verify_notified:
            error = ("发布被抖音风控拦下,需人工验证(短信验证码/扫码),但在等待时间内未完成。"
                     "请在弹窗里完成验证后重试;若窗口已关,重新点「立即发布」再操作。")
        else:
            png = await _dump(page, "unconfirmed")
            error = ("已点发布但未在页面确认到成功信号。请到抖音创作平台「作品管理」看是否已在列表"
                     "(视频常直接进审核中);若确实没发出去,把 data/debug 里最新一张 "
                     f"dy_publish_unconfirmed_*.png {'('+png+') ' if png else ''}发我校准选择器。")
    except Exception as e:
        try:
            await _dump(page, "exception")
        except Exception:
            pass
        error = f"发布异常: {e!r}"
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return ok, result_url, error
