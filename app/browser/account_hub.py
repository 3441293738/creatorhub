"""本账号管理（作品 / 关注 / 粉丝 / 私信）统一门面，具体实现按职责拆分至 hub_* 子模块。"""
from __future__ import annotations

from app.browser.hub_common import (
    _bili_cookie_str,
    _douyin_cookie_str,
    _norm_follow_user,
    _self_profile_link,
)
from app.browser.hub_works import (
    fetch_account_works,
    fetch_douyin_account_works_api,
)
from app.browser.hub_follows import (
    fetch_follows,
)
from app.browser.hub_dm import (
    fetch_dm_conversations,
    fetch_dm_history,
)
from app.browser.hub_actions import (
    do_follow,
    send_dm,
    send_dm_api,
)

__all__ = ['_bili_cookie_str', '_douyin_cookie_str', '_norm_follow_user', '_self_profile_link', 'do_follow', 'fetch_account_works', 'fetch_dm_conversations', 'fetch_dm_history', 'fetch_douyin_account_works_api', 'fetch_follows', 'send_dm', 'send_dm_api']
