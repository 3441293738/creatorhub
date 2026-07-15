"""配置加载。对应逆向 internal/config。"""
from __future__ import annotations

import os
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
    comment_recent_works: int = 5      # 监控评论时,只看每个目标最近 N 条作品
    comment_recent_days: int = 7       # 且仅限最近多少天内发布的作品
    comment_max_scrolls: int = 6       # 评论区翻页深度(滚动容器次数,越大扫得越深)
    account_check_interval_seconds: int = 1800  # 账号体检/闲置保活轮询间隔(0=关闭)
    idle_keepalive_hours: float = 6.0  # 闲置保活阈值:账号距上次活跃超此时长才摸一次(0=每轮都摸,退回旧行为)
    # 自有账号评论模式:创作中心评论管理页(实验性,抖音改版时改这里)
    creator_comment_url: str = "https://creator.douyin.com/creator-micro/interaction/comment-management"
    request_timeout_seconds: int = 20
    download_timeout_seconds: int = 120
    media_dir: str = "./data/media"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )
    # ── 多账号风控隔离 ──
    profiles_dir: str = "./data/profiles"   # 每账号持久化浏览器 profile 根目录
    max_live_contexts: int = 6              # 同时常驻的浏览器 context 上限(LRU 驱逐,控内存)
    active_accounts: int = 3                # 同一时刻最多并发活跃的账号数(错峰)
    scan_jitter: float = 0.15              # 扫描间隔随机抖动比例(±15%),消除整点齐发特征
    route_download_via_proxy: bool = True   # 媒体下载是否走账号代理(避免 CDN 拉流暴露真实 IP)
    # ── 自动评论风控闸(写操作最敏感,宁慢勿快)──
    comment_daily_cap_per_account: int = 30  # 每账号每日自动评论总上限(跨所有规则),0=不限
    comment_min_gap_seconds: int = 60        # 同账号两条评论的全局最小间隔(秒)
    comment_jitter: float = 0.4              # 评论发送时间额外抖动比例(±40%),更像真人
    comment_hourly_cap_per_account: int = 10  # 每账号每小时自动评论上限(比日上限更贴人类节律),0=不限
    # 抖音发评论用有头浏览器(弹真实窗口):抖音对无头写操作常降级/拦截,有头更稳,
    # 且能让你手动过验证码;量大嫌弹窗可设 false 试无头。
    comment_browser_headed: bool = True
    # ── 本账号写操作风控闸(取关/回关/私信,与自动评论同级)──
    #   关注/取关是抖音封号重灾区,默认比评论更保守。
    action_daily_cap_per_account: int = 20   # 每账号每日写操作总上限(跨所有动作),0=不限
    action_hourly_cap_per_account: int = 6   # 每账号每小时写操作上限,0=不限
    action_min_gap_seconds: int = 90         # 同账号两次写操作全局最小间隔(与任务级 min_gap 取大)
    # ── 活跃时段(夜间静默)──
    #   仅约束「写操作」(评论/关注/取关/私信)自动排队执行;读取抓取不受影响。
    #   按东八区(账号时区)小时判定,避免整个账号矩阵在深夜齐发的机器特征。
    quiet_hours_enabled: bool = True         # 夜间静默总开关
    active_hours_start: int = 8              # 活跃起点小时(含),0-23
    active_hours_end: int = 24               # 活跃止点小时(不含);可 >24 表示跨零点(如 25=次日 1 点)
    verify_proxy_region: bool = True         # 体检时探测代理出口国家,与账号时区不一致则告警
    # ── 本账号作品健康监控(B5:盯自己作品的流量/0播/违规,发现异常推送通知)──
    #   借鉴竞品「流速监控 / 持续0播 / 作品违规监控」。默认关闭(需 periodic 同步本账号作品,较重)。
    work_health_enabled: bool = False         # 作品健康监控总开关
    work_health_interval_seconds: int = 3600  # 每个账号多久体检一次自己作品
    work_health_zero_play_hours: float = 6.0  # 作品发布满 N 小时仍 0 播 -> 预警(限流信号)
    work_health_recent_days: int = 7          # 只体检最近 N 天内发布的作品
    work_health_stat_snapshots: bool = True   # 顺带记录账号粉丝/作品数每日快照(供「数据」趋势视图)


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
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
        cfg.db_path = (raw.get("storage", {}) or {}).get("db_path", cfg.db_path)
        px = raw.get("proxies") or []
        cfg.proxies = [str(p).strip() for p in px if str(p).strip()]
    Path(cfg.engine.media_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.engine.profiles_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
    return cfg
