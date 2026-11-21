"""X (Twitter) 网页端 `data-testid` 与 ARIA 多级容错选择器定义。"""
from __future__ import annotations

# ── 登录态与风控状态探测 ──
AUTH_READY_SELECTORS = (
    '[data-testid="SideNav_AccountSwitcher_Button"]',
    '[data-testid="AppTabBar_Profile_Link"]',
    '[data-testid="SideNav_NewTweet_Button"]',
    'a[data-testid="AppTabBar_Notifications_Link"]',
    'a[href="/notifications"]',
    'a[href="/messages"]',
    '[data-testid*="UserAvatar-Container-"]',
)

LOGIN_PROMPT_SELECTORS = (
    'input[autocomplete="username"]',
    'input[name="text"]',
    'a[data-testid="loginButton"]',
    'a[href="/login"]',
    'a[href*="/i/flow/login"]',
)

ACCOUNT_LOCKED_URL_PARTS = (
    "/account/access",
    "/i/flow/consent_flow",
)

ACCOUNT_SUSPENDED_URL_PARTS = (
    "/account/suspended",
    "/suspended",
    "/disabled",
)

SUSPENDED_BANNER_SELECTORS = (
    'div[data-testid="inlinePrompt"] a[href*="twitter-rules"]',
    '[data-testid="empty_state_header_text"]',
)

# ── 发推 / 引用 / 回复编辑器 ──
COMPOSE_TEXTBOX_SELECTORS = (
    '[data-testid="tweetTextarea_0"]',
    'div[role="dialog"] [data-testid="tweetTextarea_0"]',
    'div[role="dialog"] [contenteditable="true"][role="textbox"]',
    'div[aria-hidden="false"] [contenteditable="true"][role="textbox"]',
    '[contenteditable="true"][role="textbox"]',
)

COMPOSE_FILE_INPUT_SELECTORS = (
    'input[data-testid="fileInput"]',
    'div[role="dialog"] input[type="file"]',
    'input[type="file"][accept*="image"]',
    'input[type="file"][accept*="video"]',
    'input[type="file"]',
)

COMPOSE_ATTACHMENT_PREVIEW_SELECTORS = (
    '[data-testid="attachments"]',
    '[data-testid="mediaAttachment"]',
    'div[role="dialog"] img[src^="blob:"]',
    'div[role="dialog"] video',
)

COMPOSE_SUBMIT_BUTTON_SELECTORS = (
    'button[data-testid="tweetButton"]:not([disabled]):not([aria-disabled="true"])',
    'button[data-testid="tweetButtonInline"]:not([disabled]):not([aria-disabled="true"])',
)

COMPOSE_SUBMIT_ANY_SELECTORS = (
    'button[data-testid="tweetButton"]',
    'button[data-testid="tweetButtonInline"]',
)

REPLY_AUDIENCE_TRIGGER_SELECTORS = (
    'button[aria-label*="Everyone can reply" i]',
    'button[aria-label*="can reply" i]',
    'button[aria-label*="可以回复"]',
    '[data-testid="replyRestrictionSelector"]',
)

COMPOSE_TOAST_SELECTORS = (
    '[data-testid="toast"]',
    'div[role="alert"]',
)

# ── 推文卡片与详情页提取 ──
TWEET_ARTICLE_SELECTORS = (
    'article[data-testid="tweet"]',
    'div[data-testid="cellInnerDiv"] article',
)

TWEET_TEXT_SELECTORS = (
    '[data-testid="tweetText"]',
)

USER_CELL_SELECTORS = (
    '[data-testid="UserCell"]',
    'button[data-testid="UserCell"]',
)

# ── 互动按钮（点赞 / 转推 / 回复 / 关注）──
LIKE_BUTTON_SELECTORS = (
    'button[data-testid="like"]',
)
UNLIKE_BUTTON_SELECTORS = (
    'button[data-testid="unlike"]',
)

RETWEET_BUTTON_SELECTORS = (
    'button[data-testid="retweet"]',
)
UNRETWEET_BUTTON_SELECTORS = (
    'button[data-testid="unretweet"]',
)
RETWEET_CONFIRM_SELECTORS = (
    '[data-testid="retweetConfirm"]',
    'div[role="menuitem"][data-testid="retweetConfirm"]',
)
QUOTE_MENUITEM_SELECTORS = (
    'a[href="/compose/post"][role="menuitem"]',
    'a[href*="/compose/"][role="menuitem"]',
    'div[role="menuitem"]:has-text("Quote")',
    'div[role="menuitem"]:has-text("引用")',
)

REPLY_BUTTON_SELECTORS = (
    'button[data-testid="reply"]',
)

FOLLOW_BUTTON_SELECTORS = (
    '[data-testid="placementTracking"] button[data-testid$="-follow"]',
    'button[data-testid$="-follow"]',
)
UNFOLLOW_BUTTON_SELECTORS = (
    '[data-testid="placementTracking"] button[data-testid$="-unfollow"]',
    'button[data-testid$="-unfollow"]',
)
CONFIRM_SHEET_BUTTON_SELECTORS = (
    'button[data-testid="confirmationSheetConfirm"]',
    '[data-testid="confirmationSheetConfirm"]',
)

# ── 私信 (Messages / DM) ──
DM_PROFILE_BUTTON_SELECTORS = (
    'button[data-testid="sendDMFromProfile"]',
    '[data-testid="sendDMFromProfile"]',
    'a[href*="/messages/compose"]',
)

DM_CONVERSATION_ITEM_SELECTORS = (
    '[data-testid="conversation"]',
    'div[data-testid="cellInnerDiv"] [data-testid="conversation"]',
)

DM_MESSAGE_ENTRY_SELECTORS = (
    '[data-testid="messageEntry"]',
    '[data-testid="tweetText"]',
)

DM_COMPOSER_INPUT_SELECTORS = (
    '[data-testid="dmComposerTextInput"]',
    'aside [contenteditable="true"][role="textbox"]',
    '[data-testid="DmScrollerContainer"] ~ * [contenteditable="true"][role="textbox"]',
    '[contenteditable="true"][role="textbox"]',
    'textarea',
)

DM_SEND_BUTTON_SELECTORS = (
    'button[data-testid="dmComposerSendButton"]:not([disabled]):not([aria-disabled="true"])',
    '[data-testid="dmComposerSendButton"]',
    'button[aria-label="Send" i]:not([disabled])',
    'button[aria-label*="发送"]:not([disabled])',
)

DM_PIN_PROMPT_SELECTORS = (
    'input[data-testid*="pin" i]',
    'div[data-testid*="pin" i] input',
    'input[autocomplete="one-time-code"]',
)
