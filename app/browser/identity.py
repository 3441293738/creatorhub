"""账号设备/网络画像(Identity)。

多账号防关联的核心:每个账号一套**独立且永久固定**的浏览器画像 ——
持久化 profile 目录、固定 UA / 视口 / 时区、专属代理、确定性指纹种子。
画像在登录/建号时生成一次,之后不再变化(指纹漂移本身也是风控信号)。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# 真实机型 User-Agent 池(Windows/Mac Chrome,版本接近主流)。
# 一号选定一条后固定;切勿频繁变更。
UA_POOL: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

# 常见桌面分辨率(视口)
VIEWPORTS = [(1280, 800), (1366, 768), (1440, 900), (1536, 864), (1600, 900), (1920, 1080)]

# 国内账号默认东八区；海外平台默认美东英语环境(实际登录/绑定代理后会按代理出口 IP 精确覆盖)
DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_LOCALE = "zh-CN"
OVERSEAS_DEFAULT_TZ = "America/New_York"
OVERSEAS_DEFAULT_LOCALE = "en-US"
OVERSEAS_PLATFORMS = frozenset({"tiktok", "rednote", "instagram", "twitter"})

# 国内主要城市经纬度池(国内平台未探测到代理坐标时的兜底)
_CITY_GEO = [
    (39.9042, 116.4074),  # 北京
    (31.2304, 121.4737),  # 上海
    (23.1291, 113.2644),  # 广州
    (22.5431, 114.0579),  # 深圳
    (30.2741, 120.1551),  # 杭州
    (30.5728, 104.0668),  # 成都
    (34.3416, 108.9398),  # 西安
    (29.5630, 106.5516),  # 重庆
    (32.0603, 118.7969),  # 南京
    (30.5928, 114.3055),  # 武汉
    (38.9140, 121.6147),  # 大连
    (36.0671, 120.3826),  # 青岛
    (28.2282, 112.9388),  # 长沙
    (26.0745, 119.2965),  # 福州
    (24.4798, 118.0894),  # 厦门
    (43.8171, 125.3235),  # 长春
]

# 海外平台兜底经纬度池(与海外默认时区/语言保持地域自洽，避免海外平台暴露中国大陆坐标)
_OVERSEAS_GEO = [
    (40.7128, -74.0060),  # New York
    (39.9526, -75.1652),  # Philadelphia
    (42.3601, -71.0589),  # Boston
    (38.9072, -77.0369),  # Washington DC
    (33.7490, -84.3880),  # Atlanta
    (25.7617, -80.1918),  # Miami
    (43.6532, -79.3832),  # Toronto
    (39.9612, -82.9988),  # Columbus
]


def default_tz_for_platform(platform: str = "") -> str:
    pf = str(platform or "").strip().lower()
    return OVERSEAS_DEFAULT_TZ if pf in OVERSEAS_PLATFORMS else DEFAULT_TZ


def default_locale_for_platform(platform: str = "") -> str:
    pf = str(platform or "").strip().lower()
    return OVERSEAS_DEFAULT_LOCALE if pf in OVERSEAS_PLATFORMS else DEFAULT_LOCALE


@dataclass
class Identity:
    """一个账号的完整浏览器画像。account_id=None 表示匿名(未绑定账号的公开抓取)。"""
    account_id: Optional[int]
    profile_dir: str
    identity_mode: str = "legacy"
    browser_backend: str = "default"
    proxy: str = ""
    ua: str = ""
    viewport_w: int = 1280
    viewport_h: int = 800
    timezone_id: str = DEFAULT_TZ
    locale: str = DEFAULT_LOCALE
    fp_seed: str = ""
    fp_platform: str = ""
    fp_platform_version: str = ""
    fp_brand: str = ""
    fp_brand_version: str = ""
    fp_hardware_concurrency: int = 0
    fp_gpu_vendor: str = ""
    fp_gpu_renderer: str = ""
    fp_accept_languages: str = ""
    fp_disable_spoofing: str = ""
    fp_language_mode: str = "auto"
    fp_timezone_mode: str = "auto"
    fp_viewport_mode: str = "auto"
    fp_location_mode: str = "auto"
    fp_geolocation_permission: str = "allow"
    fp_webrtc_mode: str = "conceal"
    fp_extra_args: str = ""
    # geolocation 伪造坐标(与代理 IP 归属地/时区对齐)。0 表示未设定,由 seed 派生兜底。
    geo_lat: float = 0.0
    geo_lon: float = 0.0
    # 迁移桥:首次为存量账号创建持久 profile 时,把这些登录态 Cookie 注入进去。
    bridge_states: tuple = ()
    # 仅用于选择浏览器后端；放在末尾保持现有位置参数构造兼容。
    platform: str = ""
    # fingerprint_chromium 下选择的具体本地内核；空表示跟随默认内核。
    browser_runtime_id: str = ""
    # 扫码页已经观察到的当前用户资料，仅在本次登录内存中传递；不参与指纹签名。
    observed_login_profile: dict = field(default_factory=dict, repr=False)

    @property
    def key(self):
        if self.account_id is not None:
            return self.account_id
        # Concurrent fresh-login flows each own a different temporary profile.
        # A single ``_anon`` key would reuse or close another login's Chrome.
        profile = os.path.normcase(str(Path(self.profile_dir).resolve()))
        digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:16]
        return f"_anon:{digest}"

    @property
    def geolocation(self) -> dict:
        """返回 Patchright/CDP context 用的 geolocation 坐标(有实测坐标用实测,否则按平台与时区派生)。"""
        lat, lon = self.geo_lat, self.geo_lon
        if not lat or not lon:
            overseas = (
                str(self.platform or "").strip().lower() in OVERSEAS_PLATFORMS
                or (
                    bool(self.timezone_id)
                    and not str(self.timezone_id).startswith(("Asia/Shanghai", "Asia/Chongqing", "Asia/Urumqi"))
                )
            )
            lat, lon = derive_geo(self.fp_seed or "0", overseas=overseas)
        return {"latitude": lat, "longitude": lon, "accuracy": 60}

    @classmethod
    def from_account(cls, acc, profiles_root: str, default_ua: str) -> "Identity":
        pf = str(getattr(acc, "platform", "") or "")
        pdir = acc.profile_dir or str(Path(profiles_root) / f"acc_{acc.id}")
        bridge = tuple(s for s in (getattr(acc, "storage_state", ""),
                                   getattr(acc, "creator_storage_state", "")) if s)
        tz = getattr(acc, "timezone_id", "") or default_tz_for_platform(pf)
        loc = getattr(acc, "locale", "") or default_locale_for_platform(pf)
        # 修正历史存量海外账号未被 IP 校准前遗留的默认东八区/zh-CN 标签
        if (
            pf.lower() in OVERSEAS_PLATFORMS
            and not getattr(acc, "fp_source_ip", "")
            and not getattr(acc, "exit_timezone", "")
            and getattr(acc, "fp_timezone_mode", "auto") != "custom"
            and tz == DEFAULT_TZ
        ):
            tz = OVERSEAS_DEFAULT_TZ
        if (
            pf.lower() in OVERSEAS_PLATFORMS
            and not getattr(acc, "fp_source_ip", "")
            and getattr(acc, "fp_language_mode", "auto") != "custom"
            and loc == DEFAULT_LOCALE
        ):
            loc = OVERSEAS_DEFAULT_LOCALE
        return cls(
            account_id=acc.id, profile_dir=pdir,
            platform=pf,
            identity_mode=getattr(acc, "identity_mode", "legacy") or "legacy",
            browser_backend=(
                getattr(acc, "browser_backend", "default") or "default"),
            browser_runtime_id=(
                getattr(acc, "browser_runtime_id", "") or ""),
            proxy=acc.proxy or "",
            ua=acc.ua or default_ua,
            viewport_w=acc.viewport_w or 1280, viewport_h=acc.viewport_h or 800,
            timezone_id=tz,
            locale=loc,
            fp_seed=acc.fp_seed or seed_from_id(acc.id),
            fp_platform=getattr(acc, "fp_platform", "") or "",
            fp_platform_version=(
                getattr(acc, "fp_platform_version", "") or ""),
            fp_brand=getattr(acc, "fp_brand", "") or "",
            fp_brand_version=getattr(acc, "fp_brand_version", "") or "",
            fp_hardware_concurrency=(
                getattr(acc, "fp_hardware_concurrency", 0) or 0),
            fp_gpu_vendor=getattr(acc, "fp_gpu_vendor", "") or "",
            fp_gpu_renderer=getattr(acc, "fp_gpu_renderer", "") or "",
            fp_accept_languages=(
                getattr(acc, "fp_accept_languages", "") or ""),
            fp_disable_spoofing=(
                getattr(acc, "fp_disable_spoofing", "") or ""),
            fp_language_mode=(
                getattr(acc, "fp_language_mode", "auto") or "auto"),
            fp_timezone_mode=(
                getattr(acc, "fp_timezone_mode", "auto") or "auto"),
            fp_viewport_mode=(
                getattr(acc, "fp_viewport_mode", "auto") or "auto"),
            fp_location_mode=(
                getattr(acc, "fp_location_mode", "auto") or "auto"),
            fp_geolocation_permission=(
                getattr(acc, "fp_geolocation_permission", "allow") or "allow"),
            fp_webrtc_mode=(
                getattr(acc, "fp_webrtc_mode", "conceal") or "conceal"),
            fp_extra_args=getattr(acc, "fp_extra_args", "") or "",
            geo_lat=getattr(acc, "geo_lat", 0.0) or 0.0,
            geo_lon=getattr(acc, "geo_lon", 0.0) or 0.0,
            bridge_states=bridge,
        )


def seed_from_id(account_id) -> str:
    """没有显式种子时,用账号 id 派生一个稳定种子(保证同账号每次指纹一致)。"""
    return hashlib.md5(f"creatorhub-acc-{account_id}".encode()).hexdigest()


def derive_geo(seed: str, *, overseas: bool = False) -> tuple:
    """按 seed 确定性派生经纬度(国内/海外分池 + ±0.05° 抖动)。"""
    rnd = random.Random(f"geo-{seed}-{'intl' if overseas else 'cn'}")
    pool = _OVERSEAS_GEO if overseas else _CITY_GEO
    lat, lon = rnd.choice(pool)
    return (round(lat + rnd.uniform(-0.05, 0.05), 6),
            round(lon + rnd.uniform(-0.05, 0.05), 6))


def generate_identity_fields(platform: str = "") -> dict:
    """生成一套全新的画像字段(建号/登录时调用一次,写库后永久固定)。"""
    seed = uuid.uuid4().hex
    rnd = random.Random(seed)
    w, h = rnd.choice(VIEWPORTS)
    return {
        "ua": rnd.choice(UA_POOL),
        "viewport_w": w, "viewport_h": h,
        "timezone_id": default_tz_for_platform(platform),
        "locale": default_locale_for_platform(platform),
        "fp_seed": seed,
    }


_WEBGL_WIN = [
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.3699)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.3699)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.3623)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.4633)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Ti Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.4633)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.4629)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.4629)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.4665)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.4665)"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce RTX 4090 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.4665)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11-27.20.100.9316)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.101.2115)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) UHD Graphics 750 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.101.4502)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.101.4577)"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) Arc(TM) A770 Graphics Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.101.4952)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.21921.1000)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 5700 XT Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.21921.1000)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.24002.92)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 6700 XT Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.24002.92)"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 7900 XT Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.24033.1003)"),
]
_WEBGL_MAC = [
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M1 Pro, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M1 Max, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M2 Pro, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Max, Unspecified Version)"),
    ("Google Inc. (Intel Inc.)",
     "ANGLE (Intel Inc., Intel(R) Iris(TM) Plus Graphics OpenGL Engine, OpenGL 4.1)"),
    ("Google Inc. (Intel Inc.)",
     "ANGLE (Intel Inc., Intel(R) UHD Graphics 630 OpenGL Engine, OpenGL 4.1)"),
]


def _platform_bits(ua: str):
    """由 UA 推断 navigator.platform / userAgentData.platform / platformVersion / WebGL 池。"""
    if "Mac OS" in ua or "Macintosh" in ua:
        return "MacIntel", "macOS", "15.1.0", _WEBGL_MAC
    if "Linux" in ua and "Android" not in ua:
        return "Linux x86_64", "Linux", "6.5.0", _WEBGL_WIN
    return "Win32", "Windows", "10.0.0", _WEBGL_WIN


def fingerprint_script(seed: str, ua: str = "", locale: str = "zh-CN") -> str:
    """基于 seed 确定性派生的指纹注入脚本(add_init_script),同账号每次一致。
    包含 Function.prototype.toString 原生代码伪装([native code])、OffscreenCanvas 与 toBlob 覆盖。
    """
    rnd = random.Random(seed)
    hw = rnd.choice([4, 6, 8, 12, 16])
    mem = rnd.choice([4, 8, 16])
    noise = [rnd.randint(0, 7) for _ in range(8)]
    noise_js = json.dumps(noise)
    audio_noise = rnd.uniform(1e-7, 1e-6)
    platform, ua_data_plat, plat_ver, webgl_pool = _platform_bits(ua)
    gl_vendor, gl_renderer = rnd.choice(webgl_pool)
    loc = str(locale or "zh-CN").strip() or "zh-CN"
    base_lang = loc.split("-", 1)[0]
    langs = [loc] if base_lang == loc else [loc, base_lang]
    v = "131"
    m = re.search(r"Chrome/(\d+)", ua or "")
    if m:
        v = m.group(1)
    is_edge = "Edg/" in ua
    if is_edge:
        brands_js = (f'[{{"brand":"Chromium","version":"{v}"}},'
                     f'{{"brand":"Microsoft Edge","version":"{v}"}},'
                     f'{{"brand":"Not?A_Brand","version":"99"}}]')
    else:
        brands_js = (f'[{{"brand":"Chromium","version":"{v}"}},'
                     f'{{"brand":"Google Chrome","version":"{v}"}},'
                     f'{{"brand":"Not?A_Brand","version":"99"}}]')
    return f"""
(() => {{
  const cloaked = new WeakMap();
  const rawToString = Function.prototype.toString;
  const patchedToString = function toString() {{
    if (cloaked.has(this)) return cloaked.get(this);
    return rawToString.apply(this, arguments);
  }};
  cloaked.set(patchedToString, 'function toString() {{ [native code] }}');
  try {{
    Object.defineProperty(Function.prototype, 'toString', {{
      value: patchedToString, writable: true, configurable: true
    }});
  }} catch (e) {{}}
  const cloak = (fn, name) => {{
    try {{
      Object.defineProperty(fn, 'name', {{ value: name, configurable: true }});
      cloaked.set(fn, `function ${{name}}() {{ [native code] }}`);
    }} catch (e) {{}}
    return fn;
  }};
  const def = (o, k, v) => {{ try {{
    const getter = cloak(() => v, `get ${{k}}`);
    Object.defineProperty(o, k, {{get: getter, configurable: true}});
  }} catch (e) {{}} }};

  const navProto = Object.getPrototypeOf(navigator) || navigator;
  def(navProto, 'webdriver', false);
  def(navProto, 'hardwareConcurrency', {hw});
  def(navProto, 'deviceMemory', {mem});
  def(navProto, 'platform', {json.dumps(platform)});
  def(navProto, 'language', {json.dumps(loc)});
  def(navProto, 'languages', Object.freeze({json.dumps(langs)}));

  try {{
    const brands = {brands_js};
    const getHighEntropyValues = cloak((hints) => Promise.resolve({{
      brands: brands, mobile: false, platform: {json.dumps(ua_data_plat)},
      platformVersion: {json.dumps(plat_ver)}, architecture: "x86", bitness: "64",
      uaFullVersion: "{v}.0.0.0",
      fullVersionList: brands.map(b => ({{brand: b.brand, version: b.version + '.0.0.0'}})),
    }}), 'getHighEntropyValues');
    const toJSON = cloak(() => ({{
      brands: brands, mobile: false, platform: {json.dumps(ua_data_plat)}
    }}), 'toJSON');
    const uad = Object.freeze({{
      brands: brands, mobile: false, platform: {json.dumps(ua_data_plat)},
      getHighEntropyValues, toJSON,
    }});
    def(navProto, 'userAgentData', uad);
  }} catch (e) {{}}

  try {{
    const patchGl = (proto) => {{
      if (!proto || !proto.getParameter) return;
      const gp = proto.getParameter;
      const wrapped = cloak(function getParameter(p) {{
        if (p === 37445) return {json.dumps(gl_vendor)};
        if (p === 37446) return {json.dumps(gl_renderer)};
        return gp.apply(this, arguments);
      }}, 'getParameter');
      Object.defineProperty(proto, 'getParameter', {{
        value: wrapped, writable: true, configurable: true
      }});
    }};
    if (typeof WebGLRenderingContext !== 'undefined') patchGl(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined') patchGl(WebGL2RenderingContext.prototype);
  }} catch (e) {{}}

  const NOISE = {noise_js};
  const addNoise = (data) => {{
    for (let i = 0; i < data.length; i += 4) {{
      data[i] = (data[i] + NOISE[(i >> 2) % NOISE.length]) & 0xff;
    }}
  }};
  try {{
    const _gid = CanvasRenderingContext2D.prototype.getImageData;
    const wrappedGid = cloak(function getImageData() {{
      const d = _gid.apply(this, arguments);
      try {{ addNoise(d.data); }} catch (e) {{}}
      return d;
    }}, 'getImageData');
    Object.defineProperty(CanvasRenderingContext2D.prototype, 'getImageData', {{
      value: wrappedGid, writable: true, configurable: true
    }});
    if (typeof OffscreenCanvasRenderingContext2D !== 'undefined') {{
      const _ogid = OffscreenCanvasRenderingContext2D.prototype.getImageData;
      const wrappedOgid = cloak(function getImageData() {{
        const d = _ogid.apply(this, arguments);
        try {{ addNoise(d.data); }} catch (e) {{}}
        return d;
      }}, 'getImageData');
      Object.defineProperty(OffscreenCanvasRenderingContext2D.prototype, 'getImageData', {{
        value: wrappedOgid, writable: true, configurable: true
      }});
    }}
    const cloneNoisyCanvas = (src) => {{
      const w = src.width, h = src.height;
      if (!w || !h) return null;
      const c = document.createElement('canvas');
      c.width = w; c.height = h;
      const cx = c.getContext('2d');
      cx.drawImage(src, 0, 0);
      const d = _gid.apply(cx, [0, 0, w, h]);
      addNoise(d.data);
      cx.putImageData(d, 0, 0);
      return c;
    }};
    const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
    Object.defineProperty(HTMLCanvasElement.prototype, 'toDataURL', {{
      value: cloak(function toDataURL() {{
        try {{
          const c = cloneNoisyCanvas(this);
          if (c) return _toDataURL.apply(c, arguments);
        }} catch (e) {{}}
        return _toDataURL.apply(this, arguments);
      }}, 'toDataURL'),
      writable: true, configurable: true
    }});
    const _toBlob = HTMLCanvasElement.prototype.toBlob;
    if (_toBlob) {{
      Object.defineProperty(HTMLCanvasElement.prototype, 'toBlob', {{
        value: cloak(function toBlob() {{
          try {{
            const c = cloneNoisyCanvas(this);
            if (c) return _toBlob.apply(c, arguments);
          }} catch (e) {{}}
          return _toBlob.apply(this, arguments);
        }}, 'toBlob'),
        writable: true, configurable: true
      }});
    }}
  }} catch (e) {{}}

  try {{
    const seen = new WeakSet();
    const _gcd = AudioBuffer.prototype.getChannelData;
    Object.defineProperty(AudioBuffer.prototype, 'getChannelData', {{
      value: cloak(function getChannelData() {{
        const d = _gcd.apply(this, arguments);
        if (!seen.has(this)) {{
          seen.add(this);
          try {{ for (let i = 0; i < d.length; i += 100) d[i] += {audio_noise:.10f}; }} catch (e) {{}}
        }}
        return d;
      }}, 'getChannelData'),
      writable: true, configurable: true
    }});
  }} catch (e) {{}}
}})();
"""
