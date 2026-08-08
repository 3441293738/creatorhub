from pathlib import Path


APP_JS = Path(__file__).parents[1] / "app" / "web" / "app.js"


def test_xhs_note_card_uses_chinese_status_label_mapper():
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function noteCard(r)")
    end = source.index("function renderContentPager", start)
    note_card = source[start:end]

    assert "${contentStatusLabel(r.download_status)}" in note_card
    assert ">${r.download_status}${r.error" not in note_card
