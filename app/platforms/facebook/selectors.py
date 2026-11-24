"""Facebook 网页端多语言容错选择器定义。

优先使用语义属性（`role`、`aria-label`、`data-pagelet`、`href`、`data-testid`），避免脆弱的混淆样式类单点依赖。
"""
from __future__ import annotations

# ── 登录态、锁定与风控状态探测 ──
AUTH_READY_SELECTORS = (
    'div[role="navigation"] a[href*="profile.php"]',
    'div[role="navigation"] a[href*="/me/"]',
    'div[role="banner"] a[aria-label*="Home" i]',
    'div[role="banner"] a[aria-label*="首页"]',
    'div[role="banner"] a[aria-label*="首頁"]',
    'div[role="navigation"] > span div[role="button"]',
    'div[aria-label="Account" i][role="button"]',
    'div[aria-label="Your profile" i][role="button"]',
    'div[aria-label="你的个人主页"][role="button"]',
    'div[aria-label="你的個人檔案"][role="button"]',
)

LOGIN_PROMPT_SELECTORS = (
    'input[type="password"]',
    'form[action*="/login/"] input[name="email"]',
    'form[action*="login/device-based/login/caa/"]',
    'button[name="login"]',
    '#loginbutton',
)

ACCOUNT_LOCKED_URL_PARTS = (
    "/checkpoint",
    "/two_step_verification",
    "/login/identify",
    "/recover/",
)

ACCOUNT_DISABLED_URL_PARTS = (
    "/disabled",
    "/suspended",
)

ACCOUNT_BLOCKED_DOM_SELECTORS = (
    'div[class*="x14lw9ws xh8yej3"]',
)

RESTRICTED_DIALOG_SELECTORS = (
    'div[role="dialog"] a[href*="help/contact/"]',
    'div[role="dialog"] a[href*="/communitystandards"]',
)

# ── 弹窗、Cookie 同意与 PIN 提示清理 ──
COOKIE_ACCEPT_SELECTORS = (
    'button[data-cookiebanner*="accept_button"]',
    '[data-testid="cookie-policy-manage-dialog"] [role="dialog"] [aria-label*="all cookies" i][role="button"]:not([aria-disabled="true"])',
    '[data-testid="cookie-policy-manage-dialog"] button[data-cookiebanner*="accept"]',
    'div[role="dialog"] div[role="button"][aria-label*="Allow all cookies" i]:not([aria-disabled="true"])',
    'div[role="dialog"] div[role="button"][aria-label*="允许所有 Cookie"]:not([aria-disabled="true"])',
    'div[role="dialog"] div[role="button"][aria-label*="允許所有 Cookie"]:not([aria-disabled="true"])',
)

# ── 发帖编辑器（个人主页 / 公共主页 / 群组）──
COMPOSER_TRIGGER_SELECTORS = (
    'div[data-pagelet="ProfileComposer"] div[role="button"][tabindex="0"]',
    'div[role="region"] a[href*="profile.php"] + div[role="button"]',
    'div[role="main"] div[role="button"]:has-text("What\'s on your mind")',
    'div[role="main"] div[role="button"]:has-text("Write something")',
    'div[role="main"] div[role="button"]:has-text("分享新鲜事")',
    'div[role="main"] div[role="button"]:has-text("分享你的想法")',
    'div[role="main"] div[role="button"]:has-text("留点什么")',
    'div[role="main"] div[role="button"]:has-text("在想些什么")',
    'div[role="main"] div[role="button"]:has-text("在想什麼")',
    'div[role="main"] div[role="button"]:has-text("写点什么")',
    'div[role="main"] div[role="button"]:has-text("寫點什麼")',
)

COMPOSER_DIALOG_FORM_SELECTORS = (
    'div[role="dialog"] form[method="POST"]',
    'div[role="dialog"] form',
)

COMPOSER_TEXTBOX_SELECTORS = (
    'div[role="dialog"] form div[role="textbox"][contenteditable="true"]',
    'div[role="dialog"] div[role="textbox"][contenteditable="true"]',
    'form[method="POST"] div[role="textbox"][contenteditable="true"]',
    'div[role="dialog"] div[contenteditable="true"]',
)

COMPOSER_FILE_INPUT_SELECTORS = (
    'div[role="dialog"] form input[type="file"]',
    'div[role="dialog"] input[type="file"][accept*="image"]',
    'div[role="dialog"] input[type="file"]',
    'form input[type="file"]',
)

COMPOSER_PHOTO_BUTTON_SELECTORS = (
    'div[role="dialog"] div[role="button"][aria-label*="Photo/video" i]',
    'div[role="dialog"] div[role="button"][aria-label*="照片/视频"]',
    'div[role="dialog"] div[role="button"][aria-label*="相片／影片"]',
    'div[role="dialog"] div[role="button"][aria-label*="照片"]',
)

COMPOSER_SUBMIT_BUTTON_SELECTORS = (
    'div[role="dialog"] div[aria-label="Post"][role="button"]:not([aria-disabled="true"])',
    'div[role="dialog"] div[aria-label="发帖"][role="button"]:not([aria-disabled="true"])',
    'div[role="dialog"] div[aria-label="发布"][role="button"]:not([aria-disabled="true"])',
    'div[role="dialog"] div[aria-label="發佈"][role="button"]:not([aria-disabled="true"])',
    'div[role="dialog"] div[aria-label="Publicar"][role="button"]:not([aria-disabled="true"])',
    'div[role="dialog"] div[aria-label="Next"][role="button"]:not([aria-disabled="true"])',
    'div[role="dialog"] div[aria-label="下一步"][role="button"]:not([aria-disabled="true"])',
    'div[role="dialog"] form div[role="button"]:has-text("Post"):not([aria-disabled="true"])',
    'div[role="dialog"] form div[role="button"]:has-text("发帖"):not([aria-disabled="true"])',
    'div[role="dialog"] form div[role="button"]:has-text("发布"):not([aria-disabled="true"])',
    'div[role="dialog"] form div[role="button"]:has-text("發佈"):not([aria-disabled="true"])',
)

COMPOSER_DONE_BUTTON_SELECTORS = (
    'div[role="dialog"] div[role="button"][aria-label="Done"]:not([aria-disabled="true"])',
    'div[role="dialog"] div[role="button"][aria-label*="完成"]:not([aria-disabled="true"])',
    'div[role="dialog"] div[role="button"][aria-label="Save"]:not([aria-disabled="true"])',
    'div[role="dialog"] div[role="button"][aria-label*="保存"]:not([aria-disabled="true"])',
    'div[role="dialog"] div[role="button"][aria-label*="儲存"]:not([aria-disabled="true"])',
)

PRIVACY_LABELS = {
    "public": ("Public", "公开", "公開", "Público"),
    "friends": ("Friends", "好友", "朋友", "Amigos"),
    "private": ("Only me", "仅自己", "只限本人", "Solo yo"),
}

# ── 帖子卡片、点赞/表情回应、评论与回复 ──
POST_ARTICLE_SELECTORS = (
    'div[role="article"]',
    'div[data-pagelet^="FeedUnit_"]',
)

LIKE_BUTTON_SELECTORS = (
    'div[role="dialog"] div[role="button"][aria-label="Like"]',
    'div[role="dialog"] div[role="button"][aria-label="赞"]',
    'div[role="dialog"] div[role="button"][aria-label="讚"]',
    'div[role="dialog"] div[role="button"][aria-label="Me gusta"]',
    'div[role="main"] div[role="button"][aria-label="Like"]',
    'div[role="main"] div[role="button"][aria-label="赞"]',
    'div[role="main"] div[role="button"][aria-label="讚"]',
    'div[role="button"][aria-label="Like"]',
    'div[role="button"][aria-label="赞"]',
    'div[role="button"][aria-label="讚"]',
)

UNLIKE_BUTTON_SELECTORS = (
    'div[role="dialog"] div[role="button"][aria-label*="Remove Like" i]',
    'div[role="dialog"] div[role="button"][aria-label*="取消赞"]',
    'div[role="dialog"] div[role="button"][aria-label*="收回讚"]',
    'div[role="main"] div[role="button"][aria-label*="Remove Like" i]',
    'div[role="main"] div[role="button"][aria-label*="取消赞"]',
    'div[role="main"] div[role="button"][aria-label*="收回讚"]',
)

# 7 种 Facebook 互动回应多语言标签映射
REACTION_LABELS = {
    "like": ("Like", "赞", "讚", "Me gusta"),
    "love": ("Love", "大爱", "大心", "Me encanta"),
    "care": ("Care", "加油", "抱抱", "Me importa"),
    "haha": ("Haha", "哈哈", "Me divierte"),
    "wow": ("Wow", "哇", "Me asombra"),
    "sad": ("Sad", "心碎", "呜", "嗚", "Me entristece"),
    "angry": ("Angry", "怒", "Me enoja"),
}

COMMENT_OPEN_SELECTORS = (
    'div[role="button"][aria-label="Comment"]',
    'div[role="button"][aria-label^="Comment," i]',
    'div[role="button"][aria-label="评论"]',
    'div[role="button"][aria-label^="评论"]',
    'div[role="button"][aria-label="留言"]',
    'div[role="button"][aria-label^="留言"]',
    'div[role="button"][aria-label="Comentar"]',
    'div[role="button"][aria-label^="Comentar," i]',
    'div[role="button"][aria-label="Komentari"]',
    'div[role="button"][aria-label^="Komentari," i]',
    'div[role="button"][aria-label*="comments" i]:not([aria-label*="View" i]):not([aria-label*="Hide" i])',
)

COMMENT_TEXTBOX_SELECTORS = (
    'div[role="dialog"] form div[role="textbox"][contenteditable="true"]',
    'div[aria-hidden="false"] form div[role="textbox"][contenteditable="true"]',
    'div[role="main"] form div[role="textbox"][contenteditable="true"]',
    'form div[role="textbox"][contenteditable="true"]',
    'form [role="textbox"] p',
)

COMMENT_SUBMIT_SELECTORS = (
    '#focused-state-composer-submit div[role="button"]:not([aria-disabled="true"])',
    'div[id="focused-state-composer-submit"] div[aria-label]:not([aria-disabled="true"])',
    'form div[role="button"][aria-label="Comment"]:not([aria-disabled="true"])',
    'form div[role="button"][aria-label="评论"]:not([aria-disabled="true"])',
    'form div[role="button"][aria-label="留言"]:not([aria-disabled="true"])',
)

SHARE_BUTTON_SELECTORS = (
    'div[role="dialog"] div[role="button"]:has(div[data-ad-rendering-role="share_button"])',
    'div[role="main"] div[role="button"]:has(div[data-ad-rendering-role="share_button"])',
    'div[role="button"][aria-label="Share"]',
    'div[role="button"][aria-label="分享"]',
    'div[role="button"][aria-label="Send this to friends or post it on your profile."]',
)

# ── 关注 / 加好友 / 公共主页点赞 / 加入群组 ──
FOLLOW_BUTTON_SELECTORS = (
    'div[role="main"] div[role="button"][aria-label="Follow"]',
    'div[role="main"] div[role="button"][aria-label="关注"]',
    'div[role="main"] div[role="button"][aria-label="追蹤"]',
    'div[role="main"] div[role="button"][aria-label="Seguir"]',
)

FOLLOWING_BUTTON_SELECTORS = (
    'div[role="main"] div[role="button"][aria-label="Following"]',
    'div[role="main"] div[role="button"][aria-label="已关注"]',
    'div[role="main"] div[role="button"][aria-label="追蹤中"]',
    'div[role="main"] div[role="button"][aria-label="Siguiendo"]',
)

LIKE_PAGE_BUTTON_SELECTORS = (
    'div[role="main"] div[role="button"][aria-label="Like"]',
    'div[role="main"] div[role="button"][aria-label="赞"]',
    'div[role="main"] div[role="button"][aria-label="讚"]',
)

ADD_FRIEND_BUTTON_SELECTORS = (
    'div[role="main"] div[role="button"][aria-label="Add friend" i]',
    'div[role="main"] div[role="button"][aria-label="加为好友"]',
    'div[role="main"] div[role="button"][aria-label="加朋友"]',
    'div[role="main"] div[role="button"][aria-label="Agregar a amigos"]',
)

FRIENDS_MENU_BUTTON_SELECTORS = (
    'div[role="main"] div[role="button"][aria-label="Friends"]',
    'div[role="main"] div[role="button"][aria-label="好友"]',
    'div[role="main"] div[role="button"][aria-label="朋友"]',
)

JOIN_GROUP_BUTTON_SELECTORS = (
    'div[role="main"] div[role="button"][aria-label="Join group" i]',
    'div[role="main"] div[role="button"][aria-label="加入小组"]',
    'div[role="main"] div[role="button"][aria-label="加入社團"]',
    'div[role="main"] div[role="button"][aria-label="加入群组"]',
    'div[role="main"] div[role="button"][aria-label="Unirte al grupo"]',
)

JOINED_GROUP_BUTTON_SELECTORS = (
    'div[role="main"] div[role="button"][aria-label="Joined" i]',
    'div[role="main"] div[role="button"][aria-label="已加入"]',
    'div[role="main"] div[role="button"][aria-label="已成为成员"]',
)

# ── Messenger 私信 ──
PROFILE_MESSAGE_BUTTON_SELECTORS = (
    'div[role="main"] div[role="button"][aria-label*="Message" i]',
    'div[role="main"] div[role="button"][aria-label*="消息"]',
    'div[role="main"] div[role="button"][aria-label*="訊息"]',
    'div[role="main"] div[role="button"][aria-label*="发消息"]',
    'div[role="main"] div[role="button"][aria-label*="發送訊息"]',
    'div[role="main"] div[role="button"][aria-label*="mensaje" i]',
)

DM_COMPOSER_INPUT_SELECTORS = (
    'div[role="main"] div[role="textbox"][contenteditable="true"]',
    'div[aria-label*="Message" i] div[role="textbox"][contenteditable="true"]',
    'div[aria-label*="消息"] div[role="textbox"][contenteditable="true"]',
    'div[aria-label*="訊息"] div[role="textbox"][contenteditable="true"]',
    'div[aria-label] div[role="textbox"][contenteditable="true"]',
    'div[role="textbox"][contenteditable="true"]',
)

DM_FILE_INPUT_SELECTORS = (
    'div[role="main"] input[type="file"]',
    'div[aria-label] input[type="file"]',
    'input[type="file"]',
)

DM_SEND_BUTTON_SELECTORS = (
    'div[role="button"][aria-label*="Press enter to send" i]',
    'div[role="button"][aria-label*="Press Enter" i]',
    'div[role="button"][aria-label="Send" i]',
    'div[role="button"][aria-label*="发送"]',
    'div[role="button"][aria-label*="傳送"]',
    'div[role="button"][aria-label*="Enviar" i]',
)
