from pathlib import Path


APP_JS = Path(__file__).parents[1] / "app" / "web" / "app.js"
INDEX_HTML = Path(__file__).parents[1] / "app" / "web" / "index.html"


def test_xhs_note_card_uses_chinese_status_label_mapper():
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function noteCard(r)")
    end = source.index("function renderContentPager", start)
    note_card = source[start:end]

    assert "${contentStatusLabel(r.download_status)}" in note_card
    assert ">${r.download_status}${r.error" not in note_card


def test_keyword_collection_is_douyin_only_and_has_edit_action():
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'PLATFORM !== "douyin" && CURRENT_TAB === "collections"' in source
    assert 'platform: "douyin", account_id: accountId' in source
    assert 'onclick="editCollection(${job.id})"' in source
    assert '@app.put' not in html
    assert "新建抖音关键词采集" in html
    assert "小红书笔记" not in source[source.index("collections: {"):source.index("comments: {")]


def test_keyword_collection_results_have_card_layout_and_file_preview_actions():
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'id="collection-content-list"' in html
    assert "collection-result-grid" in html
    assert "function openCollectionPreview" in source
    assert "/local-media/" in Path(__file__).parents[1].joinpath("app", "main.py").read_text(encoding="utf-8")
    assert "function openCollectionFile" in source
    assert "function revealCollectionFile" in source
    assert "function copyCollectionPath" in source
    assert "站内预览" in source
    assert "collection-content-table" not in source


def test_keyword_collection_tasks_use_responsive_cards_without_clipped_actions():
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'class="collection-task-list"' in html
    assert 'class="collection-task"' in source
    assert 'class="collection-task-actions"' in source
    assert "collection-task-delete" in source
    assert "查看结果" in source
    assert "collection-job-list" not in html
    assert ".collection-task-actions .collection-task-delete { margin-left:auto; }" in html
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in html


def test_profile_refresh_does_not_report_deferred_probe_as_success():
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("async function refreshProfile(id)")
    end = source.index("async function setProxy(id)", start)
    refresh = source[start:end]

    assert "if (r.skipped)" in refresh
    assert 'r.reason ||' in refresh
    assert '"info"' in refresh
    assert "await refreshAccounts()" in refresh
