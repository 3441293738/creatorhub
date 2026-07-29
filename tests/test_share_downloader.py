from __future__ import annotations

import unittest
from unittest.mock import patch

from app.engine.share_downloader import (
    _clean_ydl_error,
    _download_format_options,
    _quality_format,
    ShareDownloader,
    ShareLinkError,
    detect_platform,
    extract_share_urls,
    is_private_url,
    normalize_share_text,
    require_share_urls,
)


class ShareURLTests(unittest.TestCase):
    def test_chinese_share_text_and_punctuation(self):
        text = "复制这段乱七八糟的内容 😄《标题》 https://v.douyin.com/AbC_12/，打开抖音看看！"
        links = extract_share_urls(text)
        self.assertEqual(links[0].url, "https://v.douyin.com/AbC_12/")
        self.assertEqual(links[0].platform, "douyin")

    def test_full_width_and_zero_width(self):
        text = "看看：ｈｔｔｐｓ：／／ｂ２３．ｔｖ／Ａｂｃ\u200b；谢谢"
        links = extract_share_urls(text)
        self.assertEqual(links[0].url, "https://b23.tv/Abc")
        self.assertEqual(links[0].platform, "bilibili")

    def test_html_and_url_encoded(self):
        text = (
            "第一条 https://example.com/watch?a=1&amp;b=2；"
            "第二条 https%3A%2F%2Fxhslink.com%2Fa1B2C"
        )
        links = extract_share_urls(text)
        self.assertEqual(links[0].url, "https://example.com/watch?a=1&b=2")
        self.assertTrue(any(x.host == "xhslink.com" for x in links))

    def test_backslash_unicode_and_escaped_slash(self):
        text = r"\u770b\u770b https:\/\/v.kuaishou.com\/Ab_cd-1 ）"
        self.assertIn("看看", normalize_share_text(text))
        links = extract_share_urls(text)
        self.assertEqual(links[0].url, "https://v.kuaishou.com/Ab_cd-1")
        self.assertEqual(links[0].platform, "kuaishou")

    def test_escaped_full_width_url(self):
        text = (
            r"\uff48\uff54\uff54\uff50\uff53\uff1a\uff0f\uff0f"
            r"\uff42\uff12\uff13\uff0e\uff54\uff56\uff0f\uff21\uff42\uff43"
        )
        self.assertEqual(extract_share_urls(text)[0].url, "https://b23.tv/Abc")

    def test_schemeless_and_dedupe(self):
        links = extract_share_urls(
            "b23.tv/xyz 同一个：https://b23.tv/xyz 另一个 www.youtube.com/watch?v=abc"
        )
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0].url, "https://b23.tv/xyz")
        self.assertEqual(links[1].platform, "youtube")

    def test_protocol_relative_url(self):
        links = extract_share_urls("分享地址：//v.douyin.com/abc/。")
        self.assertEqual(links[0].url, "https://v.douyin.com/abc/")

    def test_nested_percent_encoding(self):
        links = extract_share_urls("https%253A%252F%252Fv.douyin.com%252Fxyz%252F")
        self.assertEqual(links[0].url, "https://v.douyin.com/xyz/")

    def test_encoded_url_does_not_create_percent_host(self):
        links = extract_share_urls(
            "share=https%3A%2F%2Fwww.xiaohongshu.com%2Fexplore%2F"
            "1234567890abcdef12345678%3Fxsec_token%3Dabc"
        )
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].host, "www.xiaohongshu.com")
        self.assertEqual(links[0].platform, "xhs")

    def test_redirect_wrapper_prefers_embedded_platform_url(self):
        links = extract_share_urls(
            "https://share.example/redirect?"
            "target=https%3A%2F%2Fv.douyin.com%2Fxyz%2F"
        )
        self.assertEqual(links[0].url, "https://v.douyin.com/xyz/")
        self.assertEqual(links[1].host, "share.example")

    def test_unicode_path_is_percent_encoded(self):
        links = extract_share_urls("https://example.com/视频/测试?标题=中文。")
        self.assertIn("%E8%A7%86%E9%A2%91", links[0].url)
        self.assertIn("%E4%B8%AD%E6%96%87", links[0].url)
        self.assertNotIn("%E3%80%82", links[0].url)

    def test_missing_link(self):
        with self.assertRaises(ShareLinkError):
            require_share_urls("只有中文和表情 😀，没有链接")

    def test_platform_and_private_detection(self):
        self.assertEqual(detect_platform("https://www.douyin.com/video/1"), "douyin")
        self.assertEqual(detect_platform("https://unknown.example/video"), "generic")
        self.assertTrue(is_private_url("http://127.0.0.1:8000/video.mp4"))
        self.assertTrue(is_private_url("http://localhost/video.mp4"))
        self.assertFalse(is_private_url("https://example.com/video.mp4"))

    def test_hostname_is_not_blocked_by_fake_ip_dns(self):
        downloader = ShareDownloader("data/test", allow_private=False)
        # Clash/TUN 等工具可能把域名解析到 198.18.0.0/15 Fake-IP；
        # URL 安全检查不应主动做 DNS 解析并误伤正常平台域名。
        with patch("socket.getaddrinfo", side_effect=AssertionError("不应执行 DNS 解析")):
            downloader._check_url("https://v.douyin.com/abc/")

    def test_literal_private_ip_is_still_blocked(self):
        downloader = ShareDownloader("data/test", allow_private=False)
        with self.assertRaisesRegex(Exception, "本机或内网"):
            downloader._check_url("http://198.18.0.1/video.mp4")

    def test_douyin_profile_has_friendly_error(self):
        detail = (
            "ERROR: Unsupported URL: "
            "https://www.iesdouyin.com/share/user/MS4wLjABAAAA"
        )
        message = _clean_ydl_error(detail)
        self.assertIn("用户主页", message)
        self.assertIn("不是单条视频", message)

    def test_douyin_note_requests_logged_in_account(self):
        message = _clean_ydl_error(
            "Unsupported URL: https://www.douyin.com/note/7649588815329372665"
        )
        self.assertIn("图文作品", message)
        self.assertIn("抖音账号", message)

    def test_audio_format_never_falls_back_to_video_without_ffmpeg(self):
        self.assertEqual(_quality_format("audio", allow_merge=False), "ba")
        self.assertEqual(_download_format_options("audio"), {"format": "ba"})

    def test_audio_format_extracts_track_when_ffmpeg_is_available(self):
        options = _download_format_options("audio", "/tools/ffmpeg")
        self.assertEqual(options["format"], "ba/b")
        self.assertEqual(options["ffmpeg_location"], "/tools/ffmpeg")
        self.assertEqual(options["postprocessors"], [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "best",
        }])
        self.assertNotIn("merge_output_format", options)

    def test_video_format_keeps_mp4_merge_behavior(self):
        options = _download_format_options("highest", "/tools/ffmpeg")
        self.assertEqual(options["format"], "bv*+ba/b")
        self.assertEqual(options["merge_output_format"], "mp4")
        self.assertNotIn("postprocessors", options)


if __name__ == "__main__":
    unittest.main()
