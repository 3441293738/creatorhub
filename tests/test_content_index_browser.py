"""成品索引前端入口和小白自动流程说明。"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_content_index_is_a_first_class_local_navigation_page():
    html = (ROOT / "app/web/index.html").read_text(encoding="utf-8")
    script = (ROOT / "app/web/app.js").read_text(encoding="utf-8")
    assert 'data-tab="content-index"' in html
    assert 'data-panel="content-index"' in html
    assert "本机 AI 二创" in html
    assert "原片不上传" in html
    assert "生成文案与分镜" in html
    assert "查看精确费用并制作" in script
    assert '"文案已生成"' in script
    assert '"文案与分镜已生成"' in script
    assert "confirm_cost: true" in script
    assert "精确视频模型费用" in script
    assert "quote_id: quote.quote_id" in script
    assert "generation_id: quote.generation_id" in script
    assert "Wan 重新生成全部画面" in html
    assert "CosyVoice 生成新配音" in html
    assert "/static/app.js?v=20261105-video-safety" in html
    assert '"content-index": {' in script
    assert '"/api/content-index/process"' in script
    assert '"/api/content-index/sync"' in script


def test_page_does_not_make_manual_metadata_the_primary_action():
    html = (ROOT / "app/web/index.html").read_text(encoding="utf-8")
    assert "DeepSeek 重写标题、旁白和四段分镜" in html
    assert "无证据的车型和年份自动留空" in html
    assert "手动填写车型" not in html
