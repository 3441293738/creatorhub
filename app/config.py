"""全局配置模型与 YAML 加载校验。"""
from __future__ import annotations

import os
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml


@dataclass
class EngineConfig:
    scan_interval_seconds: int = 300
    worker_pool_size: int = 2          # 下载并发(跨作品)
    scan_concurrency: int = 2          # 同时抓取的目标数(并发浏览器上下文)
    block_media_resources: bool = False  # 屏蔽图片/视频/字体加载(省带宽但可能打断抖音 SPA 致拿不到数据,默认关)
    monitor_initial_backfill_count: int = 0  # 新增博主时回填最近 N 条;0=仅建立时间基线,-1=尽可能全量
    comment_recent_works: int = 5      # 监控评论时,只看每个目标最近 N 条作品
    comment_recent_days: int = 7       # 且仅限最近多少天内发布的作品
    comment_max_scrolls: int = 6       # 评论区翻页深度(滚动容器次数,越大扫得越深)
    danmaku_recent_works: int = 5      # 弹幕监控账号模式默认扫描的近期作品数
    danmaku_recent_days: int = 7       # 弹幕监控账号模式默认作品时间范围
    danmaku_max_scrolls: int = 6       # 弹幕抓取默认翻页/加载轮次
    danmaku_probe_step_seconds: float = 1.0  # 播放页时间轴探测步长(秒)
    danmaku_max_probe_points: int = 120       # 单条视频最多探测的时间点数
    danmaku_max_records_per_scan: int = 1000  # 单轮最多入库弹幕数,0=不限
    danmaku_max_records_total: int = 0        # 每个监控最多保留记录数,0=不限
    account_check_interval_seconds: int = 1800  # 登录态独立体检间隔(0=关闭)，不受普通活跃时间影响
    idle_keepalive_hours: float = 6.0  # 兼容历史闲置保活配置；不再用于延迟登录态体检
    # 自有账号评论模式:创作中心评论管理页(实验性,抖音改版时改这里)
    creator_comment_url: str = "https://creator.douyin.com/creator-micro/interaction/comment-management"
    # 自有账号弹幕模式:创作中心弹幕管理页(实验性,抖音改版时改这里)
    creator_danmaku_url: str = "https://creator.douyin.com/creator-micro/interaction/danmaku-management"
    request_timeout_seconds: int = 20
    douyin_captcha_wait_seconds: int = 300  # 浏览器模式遇验证时等待人工处理；API 风控直接冷却
    douyin_keyword_gap_seconds: float = 8.0  # 同一任务内相邻抖音关键词的最小停顿
    download_timeout_seconds: int = 120
    media_dir: str = "./data/media"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    # ── 多账号风控隔离 ──
    profiles_dir: str = "./data/profiles"   # 每账号持久化浏览器 profile 根目录
    max_live_contexts: int = 6              # 同时常驻的浏览器 context 上限(LRU 驱逐,控内存)
    # 全局浏览器运行时；账号可用 browser_backend 单独覆盖。
    browser_backend: str = "local"          # local | fingerprint_chromium
    fingerprint_chromium_path: str = ""     # 开源 fingerprint-chromium 的 chrome/chrome.exe
    fingerprint_chromium_root: str = ""     # 多内核扫描根目录；递归发现 chrome/chrome.exe
    fingerprint_chromium_allow_headless: bool = False  # 上游无头画像不完整，默认强制有头
    fingerprint_chromium_platform: str = "auto"       # auto | windows | linux | macos
    # 小红书浏览器:默认优先连接 CreatorHub 管理的每账号系统 Chrome CDP。
    xhs_browser_mode: str = "auto"          # auto | cdp | patchright
    # 账号浏览器默认常驻并复用同一 Profile/任务页；空闲后统一回收。旧配置
    # xhs_cdp_idle_seconds 仍兼容，0 表示仅按 LRU、显式关闭或退出回收。
    resident_browser_sessions: bool = True
    browser_session_idle_seconds: int = 1800
    xhs_cdp_idle_seconds: int | None = None
    # 小红书读取默认使用常驻签名客户端直连；browser 保留为页面兼容模式。
    xhs_read_mode: str = "api"               # api | browser
    # 海外小红书(Rednote)读取通道: hybrid=XYS/XYW+b1 直连优先、普通兼容失败回退页面(默认推荐); api=仅直连; browser=仅页面
    rednote_read_mode: str = "hybrid"        # hybrid | api | browser
    douyin_read_mode: str = "hybrid"         # 默认 API 优先/浏览器回退；api=仅 API；browser=仅浏览器
    # native=纯 Python 离线签名直连；browser=已授权页面；auto=直连优先/页面回退。
    tiktok_read_mode: str = "native"          # native | auto | browser
    # B站读写通道: hybrid=WBI/Cookie直连优先、明确风控兼容失败时回退页面; api=仅直连; browser=仅页面
    bilibili_read_mode: str = "hybrid"        # hybrid | api | browser
    bilibili_write_mode: str = "hybrid"       # hybrid | api | browser
    # Instagram 读写通道: hybrid=双栈协议直连优先、兼容失败回退页面; api=仅直连; browser=仅页面
    instagram_read_mode: str = "hybrid"       # hybrid | api | browser
    instagram_write_mode: str = "hybrid"      # hybrid | api | browser
    # X (Twitter) 读取与写入通道: hybrid=Syndication/REST直连优先、GraphQL时间线回退隔离浏览器; browser=全程隔离浏览器
    twitter_read_mode: str = "hybrid"         # hybrid | api | browser
    twitter_write_mode: str = "browser"       # browser
    # 视频号读取模式: auto=优先桌面微信原生 Finder CDP 协议、回退创作者助手页面;
    # protocol=仅原生 Finder 协议; browser=仅创作者助手页面。
    channels_read_mode: str = "auto"          # auto | protocol | browser
    channels_cdp_endpoint: str = ""           # 本地回环 CDP 端点(如 http://127.0.0.1:9222 或 ws://127.0.0.1:9422/cdp/...)
    channels_cdp_endpoint_file: str = ""      # wmpf_cdp 生成的本地控制口令端点描述文件
    channels_proxy_enabled: bool = False      # 是否使用内置本机视频号选择性 HTTPS 代理桥接
    channels_proxy_port: int = 2023           # 内置视频号本机代理监听端口(127.0.0.1)
    channels_proxy_manage_sysproxy: bool = True  # 内置代理是否自动托管 Windows 系统代理
    # 本账号管理中的独立读取通道。资料与粉丝已支持直连；其余三项保留
    # 独立开关，待协议完整可验证后无需再迁移配置。
    douyin_profile_mode: str = "hybrid"      # hybrid | api | browser
    douyin_followers_mode: str = "hybrid"    # hybrid | api | browser
    douyin_dm_sync_mode: str = "hybrid"      # hybrid | api | browser
    douyin_creator_danmaku_mode: str = "hybrid"  # hybrid | api | browser
    douyin_publish_mode: str = "browser"     # browser (api reserved)
    # browser=页面写入(默认推荐，真机拟人交互)；api=仅网页 HTTP/imapi；hybrid=API 优先、明确拒绝时页面回退。
    # 不确定的网络结果一律不回退，避免重复提交非幂等写请求。
    douyin_write_mode: str = "browser"       # 默认真机浏览器写入，避免无 DTrait 直连高频触发风控
    xhs_keyword_gap_seconds: float = 10.0    # 同一任务相邻关键词的最小停顿
    xhs_item_gap_seconds: float = 2.5         # 相邻详情/评论读取的最小停顿
    xhs_request_jitter: float = 0.35          # 上述停顿的正向随机抖动比例
    xhs_publish_mode: str = "browser"       # browser | api(API 仅为显式兼容模式)
    rednote_publish_mode: str = "browser"   # browser | api(API 为实验性 XYS+b1 纯协议直发通道)
    # 小红书 Web 私信。监控只读；命中规则后仍进入统一 DM 限速队列。
    xhs_dm_monitor_enabled: bool = False
    xhs_dm_poll_interval_seconds: int = 120
    xhs_dm_realtime_enabled: bool = True
    xhs_dm_realtime_debounce_seconds: float = 1.5
    xhs_dm_fallback_interval_seconds: int = 600
    xhs_dm_max_conversations_per_poll: int = 2
    xhs_dm_poll_jitter: float = 0.35
    xhs_dm_auto_reply_enabled: bool = False
    active_accounts: int = 3                # 同一时刻最多并发活跃的账号数(错峰)
    scan_jitter: float = 0.15              # 周期扫描额外等待 0–15%，同一轮稳定，不缩短基础周期
    initial_scan_spread_seconds: int = 60  # 新监控/规则首次自动执行额外等待 0–60 秒；0 关闭
    route_download_via_proxy: bool = True   # 媒体下载是否走账号代理(避免 CDN 拉流暴露真实 IP)
    # ── 自动评论风控闸(写操作最敏感,宁慢勿快)──
    comment_daily_cap_per_account: int = 30  # 每账号每日自动评论总上限(跨所有规则),0=不限
    comment_min_gap_seconds: int = 60        # 同账号两条评论的全局最小间隔(秒)
    comment_jitter: float = 0.4              # 评论排队间隔额外增加 0–40%，不缩短最小间隔
    comment_hourly_cap_per_account: int = 10  # 每账号每小时自动评论上限(比日上限更贴人类节律),0=不限
    comment_risk_cooldown_seconds: int = 21600  # 平台拒绝/验证后暂停该账号写操作(默认6小时)
    # 小红书评论发布通道:browser=页面操作;api=显式兼容;manual=只保留草稿。
    xhs_comment_write_mode: str = "browser"  # browser | api | manual
    # true=先存草稿,人工点击“通过”后由队列自动发布; false=生成后直接排队发布。
    xhs_comment_review_before_publish: bool = True
    # 抖音发评论用有头浏览器(弹真实窗口):抖音对无头写操作常降级/拦截,有头更稳,
    # 且能让你手动过验证码;量大嫌弹窗可设 false 试无头。
    comment_browser_headed: bool = True
    # ── 本账号写操作风控闸(取关/回关/私信,与自动评论同级)──
    #   关注/取关是抖音封号重灾区,默认比评论更保守。
    action_daily_cap_per_account: int = 20   # 每账号每日写操作总上限(跨所有动作),0=不限
    action_hourly_cap_per_account: int = 6   # 每账号每小时写操作上限,0=不限
    action_min_gap_seconds: int = 90         # 同账号两次写操作全局最小间隔(与任务级 min_gap 取大)
    # ── 活跃时段(夜间静默与人类作息节律)──
    #   默认同时约束自动写操作与后台自动抓爬/体检(手动即时查看不受影响)，
    #   并按「账号 ID + 当地日期」叠加每日独立随机苏醒/入睡偏移，彻底消除矩阵整点齐发与凌晨 3 点机器爬虫特征。
    quiet_hours_enabled: bool = True         # 夜间静默总开关
    quiet_hours_include_reads: bool = True   # 夜间静默期间同步休眠后台监控/评论采集/登录态体检
    quiet_hours_wake_jitter_minutes: int = 35  # 每账号每日独立苏醒/入睡随机偏移上限(0-120 分钟)
    active_hours_start: int = 8              # 活跃起点小时(含),0-23
    active_hours_end: int = 24               # 活跃止点小时(不含);可 >24 表示跨零点(如 25=次日 1 点)
    verify_proxy_region: bool = True         # 体检时探测代理出口国家,与账号时区不一致则告警
    # ── 代理防关联提醒策略（默认仅提醒不强拦，允许本机直连与多号共用代理）──
    strict_proxy_required: bool = False      # 兼容保留字段；已改为非阻断式风险提醒，不再强制拦截本机直连
    forbid_same_platform_shared_proxy: bool = False  # 兼容保留字段；已改为非阻断式风险提醒，不再强制拦截一号多绑
    # 新建 native 账号的写操作环境门禁。存量 legacy 账号不受这组开关影响。
    native_write_gate_enabled: bool = True
    native_write_require_system_chrome: bool = True
    native_write_require_verified_proxy: bool = True
    native_write_proxy_max_age_seconds: int = 86400
    browser_exit_probe_url: str = "https://ipinfo.io/json"
    # ── 本账号作品健康监控(B5:盯自己作品的流量/0播/违规,发现异常推送通知)──
    # 默认关闭；启用后需定期同步本账号作品，资源开销较高。
    work_health_enabled: bool = False         # 作品健康监控总开关
    work_health_interval_seconds: int = 3600  # 每个账号多久体检一次自己作品
    work_health_zero_play_hours: float = 6.0  # 作品发布满 N 小时仍 0 播 -> 预警(限流信号)
    work_health_recent_days: int = 7          # 只体检最近 N 天内发布的作品
    work_health_stat_snapshots: bool = True   # 顺带记录账号粉丝/作品数每日快照(供「数据」趋势视图)


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass
class LoggingConfig:
    level: str = "info"                     # info | debug | warning
    dir: str = "./logs"                     # 日志目录(可用 CREATORHUB_LOG_DIR 覆盖)
    max_bytes: int = 5 * 1024 * 1024        # app.log 单文件体积上限(默认 5 MB)
    backup_count: int = 4                   # app.log 滚动备份数量
    error_max_bytes: int = 2 * 1024 * 1024  # error.log 单文件体积上限(默认 2 MB)
    error_backup_count: int = 3             # error.log 滚动备份数量
    slow_request_ms: int = 3000             # 慢请求告警阈值(毫秒,0=关闭)
    log_mutations: bool = True              # 记录写请求(POST/PUT/PATCH/DELETE)轨迹,高频轮询默认静默


@dataclass
class RiskControlConfig:
    """Conservative cross-feature limits for platform-facing account activity."""
    enabled: bool = True
    mode: str = "conservative"
    network_group_concurrency: int = 1
    read_light_gap_seconds: int = 20
    read_heavy_gap_seconds: int = 60
    shared_write_gap_seconds: int = 300
    comment_min_gap_seconds: int = 600
    comment_hourly_cap: int = 3
    comment_daily_cap: int = 10
    social_min_gap_seconds: int = 900
    social_hourly_cap: int = 2
    social_daily_cap: int = 8
    dm_min_gap_seconds: int = 900
    dm_hourly_cap: int = 2
    dm_daily_cap: int = 8
    publish_min_gap_seconds: int = 7200
    publish_hourly_cap: int = 1
    publish_daily_cap: int = 3
    combined_action_hourly_cap: int = 3
    combined_action_daily_cap: int = 10
    cooldown_steps_seconds: List[int] = field(
        default_factory=lambda: [1800, 7200, 21600, 86400])
    recovery_successes: int = 3
    recovery_probe_gap_seconds: int = 600
    event_retention_days: int = 30
    # 同一网络出口下多个 native 账号在短时间内同时命中平台风险时，
    # 暂停该出口下所有 native 账号的写操作。
    network_group_risk_accounts: int = 2
    network_group_risk_window_seconds: int = 900
    network_group_cooldown_seconds: int = 7200
    # Account-wide pacing supplements, never replaces, per-operation limits.
    operation_gap_min_seconds: int = 8
    operation_gap_max_seconds: int = 25
    session_operation_limit: int = 12
    session_rest_min_seconds: int = 180
    session_rest_max_seconds: int = 480
    network_retry_jitter_seconds: int = 30  # 网络失败在至少 5 分钟退避上额外等待 0–30 秒


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    risk_control: RiskControlConfig = field(default_factory=RiskControlConfig)
    db_path: str = "./data/creatorhub.db"
    proxies: List[str] = field(default_factory=list)  # 代理池;建号时一号一代理 sticky 分配


def load_config(path: str | None = None) -> Config:
    path = path or os.environ.get("CREATORHUB_CONFIG_PATH") \
        or os.environ.get("DY_CONFIG_PATH", "config.yaml")
    cfg = Config()
    p = Path(path)
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        s = raw.get("server", {})
        cfg.server = ServerConfig(**{k: s[k] for k in ("host", "port") if k in s})
        e = raw.get("engine", {})
        cfg.engine = EngineConfig(**{k: v for k, v in e.items()
                                     if k in EngineConfig.__dataclass_fields__})
        if cfg.engine.xhs_browser_mode == "playwright":
            cfg.engine.xhs_browser_mode = "patchright"
        xhs_read_mode = str(
            cfg.engine.xhs_read_mode or "api").strip().lower()
        cfg.engine.xhs_read_mode = (
            xhs_read_mode if xhs_read_mode in {"browser", "api"}
            else "api")
        for pub_name in ("xhs_publish_mode", "rednote_publish_mode"):
            pub_val = str(getattr(cfg.engine, pub_name, "browser") or "browser").strip().lower()
            setattr(cfg.engine, pub_name, pub_val if pub_val in {"browser", "api"} else "browser")
        tiktok_read_mode = str(
            cfg.engine.tiktok_read_mode or "native").strip().lower()
        cfg.engine.tiktok_read_mode = (
            tiktok_read_mode
            if tiktok_read_mode in {"auto", "native", "browser"}
            else "native")
        channels_read_mode = str(
            cfg.engine.channels_read_mode or "auto").strip().lower()
        cfg.engine.channels_read_mode = (
            channels_read_mode
            if channels_read_mode in {"auto", "protocol", "browser"}
            else "auto")
        proxy_port = int(cfg.engine.channels_proxy_port or 2023)
        cfg.engine.channels_proxy_port = proxy_port if 1024 <= proxy_port <= 65535 else 2023
        for name, default in (
                ("rednote_read_mode", "hybrid"),
                ("bilibili_read_mode", "hybrid"),
                ("bilibili_write_mode", "hybrid"),
                ("douyin_read_mode", "hybrid"),
                ("douyin_profile_mode", "hybrid"),
                ("douyin_followers_mode", "hybrid"),
                ("douyin_dm_sync_mode", "hybrid"),
                ("douyin_creator_danmaku_mode", "hybrid"),
                ("douyin_publish_mode", "browser"),
                ("douyin_write_mode", "browser"),
                ("bilibili_read_mode", "hybrid"),
                ("bilibili_write_mode", "hybrid"),
                ("instagram_read_mode", "hybrid"),
                ("instagram_write_mode", "hybrid")):
            mode = str(getattr(cfg.engine, name, default) or default).strip().lower()
            setattr(cfg.engine, name, mode if mode in {"hybrid", "api", "browser"}
                    else default)
        cfg.engine.xhs_keyword_gap_seconds = max(
            0.0, float(cfg.engine.xhs_keyword_gap_seconds))
        cfg.engine.xhs_item_gap_seconds = max(
            0.0, float(cfg.engine.xhs_item_gap_seconds))
        cfg.engine.xhs_request_jitter = min(
            1.0, max(0.0, float(cfg.engine.xhs_request_jitter)))
        cfg.engine.xhs_dm_poll_interval_seconds = max(
            30, int(cfg.engine.xhs_dm_poll_interval_seconds))
        cfg.engine.xhs_dm_realtime_debounce_seconds = min(
            10.0, max(0.3, float(cfg.engine.xhs_dm_realtime_debounce_seconds)))
        cfg.engine.xhs_dm_fallback_interval_seconds = max(
            120, int(cfg.engine.xhs_dm_fallback_interval_seconds))
        cfg.engine.xhs_dm_max_conversations_per_poll = max(
            1, min(10, int(cfg.engine.xhs_dm_max_conversations_per_poll)))
        cfg.engine.xhs_dm_poll_jitter = min(
            1.0, max(0.0, float(cfg.engine.xhs_dm_poll_jitter)))
        for name in ("scan_jitter", "comment_jitter"):
            value = float(getattr(cfg.engine, name))
            setattr(cfg.engine, name, min(1.0, max(0.0, value)) if math.isfinite(value) else 0.0)
        cfg.engine.initial_scan_spread_seconds = max(
            0, min(600, int(cfg.engine.initial_scan_spread_seconds)))
        cfg.engine.quiet_hours_wake_jitter_minutes = max(
            0, min(120, int(cfg.engine.quiet_hours_wake_jitter_minutes)))
        cfg.engine.browser_session_idle_seconds = max(
            0, int(cfg.engine.browser_session_idle_seconds))
        if cfg.engine.xhs_cdp_idle_seconds is not None:
            cfg.engine.xhs_cdp_idle_seconds = max(
                0, int(cfg.engine.xhs_cdp_idle_seconds))
        backend = str(cfg.engine.browser_backend or "local").strip().lower()
        cfg.engine.browser_backend = (
            backend if backend in {"local", "fingerprint_chromium"}
            else "local"
        )
        fingerprint_platform = str(
            cfg.engine.fingerprint_chromium_platform or "auto"
        ).strip().lower()
        cfg.engine.fingerprint_chromium_platform = (
            fingerprint_platform
            if fingerprint_platform in {"auto", "windows", "linux", "macos"}
            else "auto"
        )
        risk = raw.get("risk_control", {}) or {}
        risk_values = {
            k: v for k, v in risk.items()
            if k in RiskControlConfig.__dataclass_fields__
        }
        mode = str(risk_values.get("mode", "conservative") or "").strip().lower()
        risk_values["mode"] = (
            mode if mode in {"conservative", "custom"} else "conservative")
        cfg.risk_control = RiskControlConfig(**risk_values)
        log_raw = raw.get("logging", {}) or {}
        if isinstance(log_raw, dict):
            log_values = {
                k: v for k, v in log_raw.items()
                if k in LoggingConfig.__dataclass_fields__
            }
            cfg.logging = LoggingConfig(**log_values)
        cfg.db_path = (raw.get("storage", {}) or {}).get("db_path", cfg.db_path)
        px = raw.get("proxies") or []
        cfg.proxies = [str(p).strip() for p in px if str(p).strip()]
    env_log_dir = os.environ.get("CREATORHUB_LOG_DIR", "").strip()
    if env_log_dir:
        cfg.logging.dir = env_log_dir
    env_log_level = os.environ.get("CREATORHUB_LOG_LEVEL", "").strip().lower()
    raw_level = env_log_level or str(cfg.logging.level or "info").strip().lower()
    cfg.logging.level = raw_level if raw_level in {"debug", "info", "warning", "error"} else "info"
    cfg.logging.max_bytes = max(512 * 1024, min(50 * 1024 * 1024, int(cfg.logging.max_bytes or 5 * 1024 * 1024)))
    cfg.logging.backup_count = max(1, min(10, int(cfg.logging.backup_count or 4)))
    cfg.logging.error_max_bytes = max(256 * 1024, min(20 * 1024 * 1024, int(cfg.logging.error_max_bytes or 2 * 1024 * 1024)))
    cfg.logging.error_backup_count = max(1, min(10, int(cfg.logging.error_backup_count or 3)))
    cfg.logging.slow_request_ms = max(0, min(60000, int(cfg.logging.slow_request_ms or 0)))
    cfg.logging.log_mutations = bool(cfg.logging.log_mutations)
    if not cfg.engine.channels_cdp_endpoint:
        cfg.engine.channels_cdp_endpoint = os.environ.get(
            "CREATORHUB_CHANNELS_CDP_ENDPOINT", ""
        ).strip()
    if not cfg.engine.channels_cdp_endpoint_file:
        cfg.engine.channels_cdp_endpoint_file = os.environ.get(
            "CREATORHUB_CHANNELS_CDP_ENDPOINT_FILE", ""
        ).strip()
    Path(cfg.engine.media_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.engine.profiles_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    return cfg
