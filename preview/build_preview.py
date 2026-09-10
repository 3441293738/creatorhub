from __future__ import annotations

import shutil
import sys
import json
from html import escape
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app" / "web"
PREVIEW = ROOT / "preview"
COMMUNITY = PREVIEW / "community"
COMMUNITY_QR = ROOT / "assets" / "community" / "wechat-group.jpg"
PERSONAL_QR = ROOT / "assets" / "community" / "wechat-personal.jpg"
GUIDE = ROOT / "guide"
GUIDE_IMAGES = (
    "overview-douyin.png", "accounts-list.png", "monitor-create.png",
    "monitor-comments.png", "share-download.png", "publish-workflow.png",
    "account-hub-dm.png", "autocomment-rules.png",
)


def build_guide(destination: Path) -> None:
    """Publish only explicitly approved public docs and sanitized screenshots."""
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "guide.css"):
        shutil.copy2(GUIDE / name, destination / name)
    images = destination / "images"
    images.mkdir(exist_ok=True)
    for name in GUIDE_IMAGES:
        shutil.copy2(ROOT / "assets" / "screenshots" / name, images / name)
    platforms = json.loads((GUIDE / "platforms.json").read_text(encoding="utf-8"))
    for slug, platform in platforms.items():
        if slug not in {"douyin", "xhs", "kuaishou", "shipinhao"}:
            raise ValueError(f"Unknown documentation platform: {slug}")
        chapters = []
        for feature in platform["features"]:
            steps = "".join(f"<li>{escape(step)}</li>" for step in feature["steps"])
            chapters.append(f'<section id="{escape(feature["anchor"])}"><h2>{escape(feature["title"])}</h2>'
                            f'<ol>{steps}</ol><a href="../#{escape(feature["anchor"])}">查看完整操作与故障处理 →</a></section>')
        links = ''.join(f'<a href="../{key}/">{escape(value["name"])}</a>' for key, value in platforms.items())
        limits = ''.join(f'<li>{escape(item)}</li>' for item in platform['limits'])
        toc = ''.join(f'<a href="#{escape(item["anchor"])}">{escape(item["title"])}</a>' for item in platform['features'])
        name = escape(platform['name'])
        html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{name}使用指南 · CreatorHub</title>
<meta name="description" content="{escape(platform['intro'])}"><link rel="stylesheet" href="../guide.css"></head><body>
<a class="skip" href="#main">跳到正文</a><header class="topbar"><a class="brand" href="../">CreatorHub <span>使用指南</span></a><nav aria-label="平台指南">{links}</nav></header>
<div class="layout"><aside><nav class="toc" aria-label="本页目录"><p>{name}入门</p><a href="#start">登录与第一个任务</a>{toc}<p>通用帮助</p><a href="../#install">安装与启动</a><a href="../#notifications">通知设置</a><a href="../#queue">任务队列与风控</a><a href="../#backup">更新与备份</a><a href="../#faq">常见问题</a><a href="../">全部功能手册</a></nav></aside>
<main id="main"><section id="start"><div class="eyebrow">PLATFORM GUIDE</div><h1>{name}使用指南</h1><p class="lead">{escape(platform['intro'])}</p>
<div class="note">真实操作需要本地运行。还没装好？先看<a href="../#install">安装教程</a>。</div>
<h2>第一步：登录账号</h2><p>{escape(platform['login'])}</p><h2>第二步：完成第一个任务</h2><p>{escape(platform['first'])}</p><a href="../#{escape(platform['firstAnchor'])}">查看图文步骤 →</a>
<h3>开始前了解这些差异</h3><ul>{limits}</ul></section>{''.join(chapters)}<footer>教程随项目维护。遇到问题先看<a href="../#faq">常见问题</a>，或返回<a href="../">指南首页</a>。</footer></main></div></body></html>'''
        folder = destination / slug
        folder.mkdir(exist_ok=True)
        (folder / "index.html").write_text(html, encoding="utf-8")


def build(destination: Path) -> None:
    destination = destination.resolve()
    if destination == ROOT or ROOT not in destination.parents:
        raise ValueError(f"preview destination must stay inside the repository: {destination}")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    html = (SOURCE / "index.html").read_text(encoding="utf-8")
    html = html.replace('data-guide-base="https://3441293738.github.io/creatorhub/guide/"',
                        'data-guide-base="./guide/" data-preview="true"')
    # The static demo does not create real tasks or durable submission receipts.
    html = html.replace('<script src="/static/submissions.js"></script>', '')
    marker = '<script src="/static/app.js"></script>'
    replacement = '<script src="./demo-api.js"></script>\n<script src="./app.js"></script>'
    if marker not in html:
        raise RuntimeError("app script marker not found in app/web/index.html")
    (destination / "index.html").write_text(html.replace(marker, replacement), encoding="utf-8")
    # Early theme boot, shared tokens and shell interactions also work on Pages.
    html = (destination / "index.html").read_text(encoding="utf-8")
    for asset in ("appearance.js", "appearance.css", "engine-settings.js", "workspace-ui.js", "workbench.js", "workbench.css", "workbench.js.LEGAL.txt", "workbench-licenses.txt"):
        html = html.replace(f"/static/{asset}", f"./{asset}")
        shutil.copy2(SOURCE / asset, destination / asset)
    (destination / "index.html").write_text(html, encoding="utf-8")
    shutil.copy2(SOURCE / "app.js", destination / "app.js")
    shutil.copy2(PREVIEW / "demo-api.js", destination / "demo-api.js")
    shutil.copytree(COMMUNITY, destination / "community")
    shutil.copy2(COMMUNITY_QR, destination / "community" / COMMUNITY_QR.name)
    shutil.copy2(PERSONAL_QR, destination / "community" / PERSONAL_QR.name)
    build_guide(destination / "guide")
    (destination / ".nojekyll").write_text("", encoding="utf-8")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "_site"
    build(target if target.is_absolute() else ROOT / target)
    print(f"Built static preview: {target}")
