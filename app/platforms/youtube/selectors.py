"""YouTube Web 与 YouTube Studio DOM 选择器集中表。

覆盖 Studio 上传、评论/回复、点赞、订阅、观看、多频道切换及 Polymer / Wiz 多语言容错选择器。
"""
from __future__ import annotations

# ── 登录态与账号状态检测 ──
AVATAR_BUTTON_SELECTORS = (
    "button#avatar-btn",
    "ytd-topbar-menu-button-renderer button#avatar-btn",
    "#avatar-btn",
)

LOGGED_IN_SIGNALS = (
    "button#avatar-btn",
    "ytd-notification-topbar-button-renderer",
    "#avatar-btn img",
)

SIGNIN_BUTTON_SELECTORS = (
    'a[href*="accounts.google.com/ServiceLogin"]',
    'ytd-button-renderer a[href*="ServiceLogin"]',
    '#buttons a[href*="ServiceLogin"]',
)

AUTH_BLOCKED_URL_TOKENS = (
    "accounts.google.com",
    "youtube.com/oops",
    "/disabled/",
    "/signin/rejected",
    "/signin/challenge",
)

# ── 频道切换（channel_switcher & 头像菜单） ──
CHANNEL_SWITCHER_ITEM_SELECTORS = (
    "ytd-account-item-renderer",
    "ytd-account-item-section-renderer ytd-account-item-renderer",
)

ACCOUNT_MENU_NAME_SELECTORS = (
    "#account-name",
    "yt-formatted-string#account-name",
)

ACCOUNT_MENU_HANDLE_SELECTORS = (
    "#channel-handle",
    "yt-formatted-string#channel-handle",
)

CREATE_CHANNEL_DIALOG_SELECTORS = (
    "ytd-channel-creation-dialog-renderer",
    "ytd-button-renderer#create-channel-button",
    "#create-channel-button button",
)

DISMISS_BUTTON_SELECTORS = (
    "#dismiss-button button",
    "#dismiss-button",
)

# ── YouTube Studio 视频上传 ──
STUDIO_UPLOAD_TRIGGER_SELECTORS = (
    "#upload-button",
    "#upload-icon",
    "ytcp-button#create-icon",
    'ytcp-icon-button[id="upload-icon"]',
)

STUDIO_SELECT_FILES_SELECTORS = (
    "#select-files-button",
    "ytcp-uploads-file-picker #select-files-button",
)

STUDIO_FILE_INPUT_SELECTORS = (
    'ytcp-uploads-file-picker input[type="file"]',
    'input[type="file"][name="Filedata"]',
    'input[type="file"]',
)

STUDIO_DETAILS_EDITOR_SELECTORS = (
    'ytcp-video-metadata-editor#details',
    'ytcp-video-metadata-editor',
)

STUDIO_TITLE_INPUT_SELECTORS = (
    '#title-textarea #textbox[contenteditable="true"]',
    'ytcp-social-suggestions-textbox#title-textarea #textbox',
    '#textbox[aria-label*="title" i]',
    '#textbox[aria-label*="标题"]',
    '#textbox',
)

STUDIO_DESC_INPUT_SELECTORS = (
    '#description-container #child-input #textbox[contenteditable="true"]',
    '#description-textarea #textbox[contenteditable="true"]',
    '#description-container #textbox',
)

STUDIO_MADE_FOR_KIDS_YES = 'tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_MFK"], [name="VIDEO_MADE_FOR_KIDS_MFK"]'
STUDIO_MADE_FOR_KIDS_NO = 'tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"], [name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]'

STUDIO_SHOW_MORE_BUTTON = "#toggle-button"
STUDIO_PAID_PROMOTION_CHECKBOX = "#has-ppp"
STUDIO_TAGS_INPUT = "#tags-container #text-input"
STUDIO_THUMBNAIL_FILE_INPUT = 'ytcp-thumbnail-uploader input[type="file"], .ytcp-thumbnail-uploader[type="file"], input#file-loader[type="file"]'
STUDIO_PLAYLIST_DROPDOWN = "ytcp-video-metadata-playlists ytcp-text-dropdown-trigger"
STUDIO_PLAYLIST_ITEMS = "ytcp-playlist-dialog #playlists-list #items ytcp-ve"
STUDIO_PLAYLIST_CHECKBOXES = "ytcp-playlist-dialog #playlists-list #items input"
STUDIO_PLAYLIST_DONE_BUTTON = "ytcp-playlist-dialog .done-button, ytcp-playlist-dialog #done-button"
STUDIO_ALLOW_EMBED_CHECKBOX = "#allow-embed ytcp-checkbox-lit"
STUDIO_NOTIFY_SUBSCRIBERS_CHECKBOX = "#notify-subscribers"

STUDIO_STEP_CHECKS = '[test-id="CHECKS"]'
STUDIO_STEP_REVIEW = '[test-id="REVIEW"]'
STUDIO_NEXT_BUTTON = "#next-button"
STUDIO_DONE_BUTTON = "#done-button"
STUDIO_DIALOG_CLOSE_SELECTORS = (
    "ytcp-uploads-dialog #close-button",
    "ytcp-uploads-dialog #close-button button",
    "#close-button button",
    'ytcp-uploads-dialog button[aria-label="Close" i]',
    'ytcp-uploads-dialog button[aria-label="关闭"]',
)
STUDIO_VIDEO_LINK_SELECTORS = (
    "a#video-link",
    "a.ytcp-video-info[href*='youtu.be']",
    "a.ytcp-video-info[href*='youtube.com']",
    ".video-url-fadeable a[href]",
)
STUDIO_VISIBILITY_RADIO = {
    "private": 'tp-yt-paper-radio-button[name="PRIVATE"]',
    "unlisted": 'tp-yt-paper-radio-button[name="UNLISTED"]',
    "public": 'tp-yt-paper-radio-button[name="PUBLIC"]',
}
STUDIO_PREMIERE_CHECKBOX = "#enable-premiere-checkbox"
STUDIO_SHARE_DIALOG_SELECTORS = (
    ".url-container.ytcp-video-share-dialog",
    "ytcp-video-share-dialog",
    "a#share-url",
)

STUDIO_METRIC_SUBSCRIBERS = 'div[class*="metric-value-big"]'

# ── 视频互动（点赞 / 点踩 / 订阅 / 评论 / 回复 / 观看） ──
WATCH_LIKE_OFF_SELECTORS = (
    '[class*="LikeButtonViewModelHost"] button[aria-pressed="false"]',
    'like-button-view-model button[aria-pressed="false"]',
    '#top-level-buttons-computed ytd-toggle-button-renderer:first-child button[aria-pressed="false"]',
)
WATCH_LIKE_ON_SELECTORS = (
    '[class*="LikeButtonViewModelHost"] button[aria-pressed="true"]',
    'like-button-view-model button[aria-pressed="true"]',
    '#top-level-buttons-computed ytd-toggle-button-renderer:first-child button[aria-pressed="true"]',
)
WATCH_DISLIKE_OFF_SELECTORS = (
    '[class*="ytDislikeButtonViewModelHost"] button[aria-pressed="false"]',
    'dislike-button-view-model button[aria-pressed="false"]',
)
WATCH_DISLIKE_ON_SELECTORS = (
    '[class*="ytDislikeButtonViewModelHost"] button[aria-pressed="true"]',
    'dislike-button-view-model button[aria-pressed="true"]',
)

SHORTS_LIKE_OFF_SELECTORS = (
    'ytd-reel-video-renderer[is-active] like-button-view-model button[aria-pressed="false"]',
    'ytd-reel-video-renderer like-button-view-model button[aria-pressed="false"]',
    'ytd-toggle-button-renderer#like-button button[aria-pressed="false"]',
)
SHORTS_LIKE_ON_SELECTORS = (
    'ytd-reel-video-renderer[is-active] like-button-view-model button[aria-pressed="true"]',
    'ytd-reel-video-renderer like-button-view-model button[aria-pressed="true"]',
    'ytd-toggle-button-renderer#like-button button[aria-pressed="true"]',
)
SHORTS_DISLIKE_OFF_SELECTORS = (
    'ytd-reel-video-renderer[is-active] dislike-button-view-model button[aria-pressed="false"]',
    'ytd-toggle-button-renderer#dislike-button button[aria-pressed="false"]',
)
SHORTS_DISLIKE_ON_SELECTORS = (
    'ytd-reel-video-renderer[is-active] dislike-button-view-model button[aria-pressed="true"]',
    'ytd-toggle-button-renderer#dislike-button button[aria-pressed="true"]',
)

# 订阅 / 已订阅按钮
SUBSCRIBE_ACTION_SELECTORS = (
    ".yt-subscribe-button-view-model-wiz__container button.yt-spec-button-shape-next--filled",
    "yt-subscribe-button-view-model button.ytSpecButtonShapeNextFilled",
    "#owner #subscribe-button-shape:not([invisible]) button",
    '#subscribe-button ytd-subscribe-button-renderer button:not([aria-label*="Unsubscribe" i]):not([aria-label*="已订阅"])',
)
SUBSCRIBED_STATE_SELECTORS = (
    ".yt-subscribe-button-view-model-wiz__container button.yt-spec-button-shape-next--tonal",
    "yt-subscribe-button-view-model button.ytSpecButtonShapeNextTonal",
    "#owner #subscribe-button-shape[invisible]",
    '#notification-preference-button button',
    'yt-subscribe-button-view-model button[aria-label*="Unsubscribe" i]',
    'yt-subscribe-button-view-model button[aria-label*="已订阅"]',
    'yt-subscribe-button-view-model button[aria-label*="已訂閱"]',
)
UNSUBSCRIBE_MENU_ITEM_SELECTORS = (
    'ytd-menu-service-item-renderer:has-text("Unsubscribe")',
    'ytd-menu-service-item-renderer:has-text("取消订阅")',
    'ytd-menu-service-item-renderer:has-text("取消訂閱")',
    'tp-yt-paper-item:has-text("Unsubscribe")',
    'tp-yt-paper-item:has-text("取消订阅")',
)
CONFIRM_DIALOG_BUTTON_SELECTORS = (
    "yt-confirm-dialog-renderer #confirm-button button",
    "#confirm-button button",
)

# 评论区与回复
COMMENTS_SECTION = "#comments"
COMMENT_SIMPLEBOX_PLACEHOLDER = "#comments #simplebox-placeholder"
COMMENT_EDITABLE_BOX = "#comments #contenteditable-root"
COMMENT_SUBMIT_BUTTON = "#comments #submit-button button:not([disabled])"

LINKED_COMMENT_ROOT = "#comment[linked], ytd-comment-view-model[is-linked-comment], ytd-comment-renderer[is-linked-comment]"
LINKED_COMMENT_REPLY_BTN = "#comment[linked] #reply-button-end button, ytd-comment-view-model[is-linked-comment] #reply-button-end button"
LINKED_COMMENT_REPLY_INPUT = "#comment[linked] #reply-dialog:not([hidden]) #contenteditable-root, ytd-comment-view-model[is-linked-comment] #reply-dialog:not([hidden]) #contenteditable-root"
LINKED_COMMENT_REPLY_SUBMIT = "#comment[linked] #reply-dialog #submit-button button:not([disabled]), ytd-comment-view-model[is-linked-comment] #reply-dialog #submit-button button:not([disabled])"

COMMENT_LIKE_OFF = "#comment #like-button button[aria-pressed='false'], ytd-comment-engagement-bar #like-button button[aria-pressed='false']"
COMMENT_LIKE_ON = "#comment #like-button button[aria-pressed='true'], ytd-comment-engagement-bar #like-button button[aria-pressed='true']"

# 播放器与广告跳过
PLAYER_VIDEO_SELECTORS = ("#player video", "ytd-player video", "video")
SKIP_AD_BUTTON_SELECTORS = (
    "#player button.ytp-skip-ad-button",
    "button.ytp-ad-skip-button-modern",
    "button.ytp-ad-skip-button",
    'button[id*="skip-button"]',
)
