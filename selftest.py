"""自检脚本(离线):验证签名原语 + 关键依赖是否就位。
  python selftest.py
注:抓取/登录现在走真实浏览器(Playwright),不再依赖自算 a_bogus,
   所以这里只做基础原语自检 + Playwright 可用性检查。
"""
import shutil
import sys


def check_primitives() -> bool:
    try:
        from app.platforms.douyin.signing import sm3_hash, rc4

        h = sm3_hash(b"abc").hex()
        assert h == "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0", "SM3 失败"
        print("[SM3] OK")
        out = bytes(rc4(b"Key", b"Plaintext")).hex().upper()
        assert out == "BBF316E8D940AF0AD3", "RC4 失败"
        print("[RC4] OK")
        return True
    except Exception as e:
        print(f"[签名原语] FAIL: {e}")
        return False


def check_risk_control() -> bool:
    try:
        from app.config import Config
        from app.risk import RiskController, network_key

        cfg = Config()
        controller = RiskController(cfg)
        assert controller.policy.enabled
        assert controller.policy.cooldown_steps_seconds == [1800, 7200, 21600, 86400]
        assert network_key("") == "direct"
        proxy_key = network_key("http://user:secret@proxy.example:8000")
        assert proxy_key.startswith("proxy:") and "secret" not in proxy_key
        print("[平台风控] OK: 持久化策略、冷却阶梯和网络出口分组可用")
        return True
    except Exception as e:
        print(f"[平台风控] FAIL: {e}")
        return False


def check_playwright() -> bool:
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"[Playwright] FAIL: 未安装: {e}")
        print("   运行: python creatorhub.py install")
        return False
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        print("[Playwright] OK: Chromium 可启动")
        return True
    except Exception as e:
        print(f"[Playwright] FAIL: 启动失败: {e}")
        print("   运行: python creatorhub.py install")
        return False


def check_node() -> bool:
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm:
        print("[Node.js] OK: 小红书发布依赖可安装/更新")
        return True
    print("[Node.js] INFO: 未检测到（仅影响小红书发布，其他功能可用）")
    return True


def check_share_downloader() -> bool:
    try:
        import yt_dlp
        from app.engine.share_downloader import extract_share_urls

        links = extract_share_urls(
            "中文分享：https%3A%2F%2Fv.douyin.com%2Fexample%2F 😄"
        )
        assert links and links[0].host == "v.douyin.com"
        print(f"[链接下载] OK: yt-dlp {yt_dlp.version.__version__}，分享文案解析正常")
        return True
    except Exception as e:
        print(f"[链接下载] FAIL: {e}")
        print("   运行: python -m pip install -r requirements.txt")
        return False


if __name__ == "__main__":
    checks = (
        check_primitives(),
        check_risk_control(),
        check_playwright(),
        check_node(),
        check_share_downloader(),
    )
    if all(checks):
        print("\n自检通过。启动:  python creatorhub.py")
        raise SystemExit(0)
    print("\n自检未通过，请先运行:  python creatorhub.py install")
    raise SystemExit(1)
