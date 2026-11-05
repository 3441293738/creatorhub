"""成品索引前端入口和小白自动流程说明。"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_content_index_is_a_first_class_local_navigation_page():
    html = (ROOT / "app/web/index.html").read_text(encoding="utf-8")
    script = (ROOT / "app/web/app.js").read_text(encoding="utf-8")
    assert 'data-tab="content-index"' in html
    assert 'data-panel="content-index"' in html
    assert "本机自动处理" in html
    assert "原片不上传" in html
    assert "自动处理全部" in html
    assert '"content-index": {' in script
    assert '"/api/content-index/process"' in script
    assert '"/api/content-index/sync"' in script


def test_page_does_not_make_manual_metadata_the_primary_action():
    html = (ROOT / "app/web/index.html").read_text(encoding="utf-8")
    assert "DeepSeek 提取标题、车型和主题" in html
    assert "百炼失败回退" in html
    assert "无口播证据的车型和年份会自动留空" in html
    assert "手动填写车型" not in html
