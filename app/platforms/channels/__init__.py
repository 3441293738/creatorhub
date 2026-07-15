"""微信视频号(WeChat Channels / finder)平台包。

⚠️ 与抖音/快手的关键差异(务必先读):
  1. 视频号的数据只能从**创作者助手** channels.weixin.qq.com 的
     `cgi-bin/mmfinderassistant-bin/*` 接口拿到,且**只有本账号自己的数据**——
     平台没有对外的公开 web 作品接口,所以视频号不做「监控别人作品」,只做
     「本账号作品 / 数据 / 评论 / 发布」。
  2. 视频号视频走加密 CDN(encfilekey + 需 decode),**基本无法直接下载搬运**,
     故 parse_channels_feed 允许返回「无可下载媒体」的 Aweme(只记元数据+统计)。
  3. 助手接口是 POST 带 body、依赖 `_finder_auth` 登录态,采用「登录态浏览器打开
     助手页 → 拦截其自身发出的响应」路线(免签名),与 ks 一致。

⚠️ 接口路径 / 响应字段 / 发布页选择器都需用**真实视频号账号**跑一遍、对照
   channels_fetcher 打印的 api_seen 诊断日志校准(见各常量注释)。
"""
from .extract import (parse_channels_feed, parse_channels_comment,
                      flatten_channels_comments, parse_self_user,
                      safe_title, Aweme, MediaItem)
from .resolve import (resolve_channels_user_id, resolve_channels_photo_id,
                      looks_like_photo)
from .publish import publish_channels

__all__ = [
    "parse_channels_feed", "parse_channels_comment", "flatten_channels_comments",
    "parse_self_user", "safe_title", "Aweme", "MediaItem",
    "resolve_channels_user_id", "resolve_channels_photo_id", "looks_like_photo",
    "publish_channels",
]
