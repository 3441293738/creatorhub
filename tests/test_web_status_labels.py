from pathlib import Path


APP_JS = Path(__file__).parents[1] / "app" / "web" / "app.js"
INDEX_HTML = Path(__file__).parents[1] / "app" / "web" / "index.html"
WORKBENCH_JSX = Path(__file__).parents[1] / "frontend" / "workbench.jsx"


def test_xhs_note_card_uses_chinese_status_label_mapper():
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function noteCard(r)")
    end = source.index("function renderContentPager", start)
    note_card = source[start:end]

    assert "${contentStatusLabel(r.download_status)}" in note_card
    assert ">${r.download_status}${r.error" not in note_card


def test_keyword_collection_supports_douyin_and_xhs_with_edit_action():
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    workbench = WORKBENCH_JSX.read_text(encoding="utf-8")

    assert '!["douyin", "xhs"].includes(PLATFORM) && CURRENT_TAB === "collections"' in source
    assert 'platform: PLATFORM, account_id: accountId' in source
    assert 'a.platform === PLATFORM' in source
    assert 'platform: job.platform, keywords' in source
    assert 'onclick="editCollection(${job.id})"' in source
    assert 'canRetry && ["douyin", "xhs"].includes(job.platform)' in source
    assert 'canRetry && job.platform === "douyin"' not in source
    assert '@app.put' not in html
    assert 'id="collection-create-title"' in html
    assert "新建${xhs ? \"小红书\" : \"抖音\"}关键词采集" in source
    assert 'platform === "xhs" ? "小红书" : "抖音"' in workbench
    assert "composerTitle(ctx.tab, ctx.platform)" in workbench
    assert "批量搜索平台作品" in source[source.index("collections: {"):source.index("comments: {")]


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


def test_monitor_advanced_filters_are_editable_and_retry_reports_real_result():
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")

    for control in (
            "t-max-scrolls", "t-max-items", "t-record-media",
            "t-recent-days", "t-min-likes", "t-min-comments",
            "t-include-keywords", "t-exclude-keywords"):
        assert f'id="{control}"' in html
    assert "高级抓取与筛选" in html
    assert 'if (!result.ok) throw new Error' in source
    assert "已重新加入下载队列" not in source[source.index(
        "async function retryDl"):source.index("async function delContent")]


def test_unified_task_queue_page_has_filters_badge_and_source_navigation():
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'data-tab="queue"' in html
    assert 'data-panel="queue"' in html
    assert 'id="tb-queue"' in html
    for control in ("queue-platform", "queue-type", "queue-state", "queue-query"):
        assert f'id="{control}"' in html
    assert 'id="queue-table"' in html
    assert 'id="queue-pager"' in html
    assert 'queue: {' in source[source.index("const PAGE_META"):source.index("function updatePageContext")]
    assert 'async function refreshTaskQueue(' in source
    assert 'async function refreshTaskQueueBadge(' in source
    assert 'openTaskQueueSource' in source
    assert '"queue"' in source[source.index("const VALID_TABS"):source.index("const LEGACY_HUB_TABS")]


def test_fingerprint_environment_check_has_status_disclosure_and_manual_action():
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "environment_check" in source
    assert "环境体检" in source
    assert "checkBrowserEnvironment" in source
    assert "/environment-check" in source
    assert "该站点会看到当前出口 IP 和浏览器指纹" in source
    assert "BrowserScan 体检标签" in html


def test_fingerprint_login_can_be_configured_before_first_browser_launch():
    source = APP_JS.read_text(encoding="utf-8")

    assert "configurePreLoginFingerprint" in source
    assert "登录前指纹配置" in source
    assert "使用此指纹登录" in source
    assert "使用出口 IP 自动配置" in source
    assert "loginStartOptions(fingerprint)" in source
    assert 'body: JSON.stringify(fingerprint)' in source


def test_kuaishou_login_copy_matches_the_automatic_qr_and_shared_publish_session():
    source = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")
    ks_login = source[source.index("async function startKsLogin()"):
                      source.index("async function startKsCreatorLogin()")]

    assert "登录二维码会自动弹出" in ks_login
    assert "完成后这里会自动识别并刷新资料" in ks_login
    assert '"请先完成「快手扫码登录」"' in source
    assert "创作平台登录（备用）" in html
    assert "请在该窗口点击「登录」" not in ks_login


def test_kuaishou_account_card_uses_human_profile_labels():
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("async function refreshAccounts()")
    end = source.index("function populateAccountSelect", start)
    refresh = source[start:end]

    assert 'isKs ? "主页ID "' in refresh
    assert 'a.following_count || 0) + " 关注"' in refresh
    assert 'a.total_favorited || 0) + " 获赞"' in refresh
    assert '(isXhs || isKs) ? "user_id "' not in refresh


def test_refresh_profile_uses_kuaishou_id_label():
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("async function refreshProfile(id)")
    end = source.index("async function setProxy", start)
    refresh = source[start:end]

    assert 'refreshedPlatform === "kuaishou" ? " · 快手号 "' in refresh


def test_kuaishou_publish_view_reuses_the_account_browser():
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("async function refreshPublish()")
    end = source.index("async function runPublish", start)
    publish = source[start:end]

    assert '["kuaishou", "shipinhao"].includes(t.platform)' in publish
    assert "快手作品管理页" in publish
    assert "不能交给系统浏览器" in publish
