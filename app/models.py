"""数据模型 (SQLModel)。精简自逆向的 DouyinAccount / MonitorTarget / ContentRecord。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class DouyinAccount(SQLModel, table=True):
    """登录得到的平台账号(浏览器会话持有者)。表名沿用历史,实际承载多平台账号。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)  # douyin | xhs
    nickname: str = ""
    sec_uid: str = ""              # 抖音 sec_uid / 小红书 user_id
    uid: str = ""                  # 抖音数字 uid(= IM device_id,用于 frontier-im WS 接收)
    douyin_id: str = ""            # 抖音号 / 小红书号(red_id)
    avatar: str = ""              # 头像
    follower_count: int = 0
    aweme_count: int = 0
    cookie: str = ""               # 原始 Cookie 串(粘贴登录时填,仅展示/兜底)
    storage_state: str = ""        # Patchright storage_state JSON(浏览器登录态)
    creator_storage_state: str = ""  # 创作中心登录态(用于自有账号评论模式)
    status: str = "active"         # active | invalid
    # ── 设备/网络画像(防多账号关联风控:登录时生成一次,之后永久固定)──
    profile_dir: str = ""          # 独立持久化用户目录(Chromium user-data-dir)
    proxy: str = ""               # 该账号专属代理 http://user:pass@host:port / socks5://...
    ua: str = ""                  # 该账号固定 User-Agent
    viewport_w: int = 1280
    viewport_h: int = 800
    timezone_id: str = "Asia/Shanghai"
    locale: str = "zh-CN"
    fp_seed: str = ""             # 指纹种子(canvas/webgl/navigator 据此确定性生成,保证每次一致)
    geo_lat: float = 0.0          # geolocation 伪造纬度(代理体检时按出口 IP 归属地写入;0=按种子派生兜底)
    geo_lon: float = 0.0          # geolocation 伪造经度
    proxy_status: str = "unknown"  # unknown | ok | bad
    # 由账号的真实浏览器 context 探测并持久化；后续写操作用它识别出口漂移。
    exit_ip: str = ""
    exit_country: str = ""
    exit_asn: str = ""
    exit_timezone: str = ""
    exit_proxy_signature: str = ""
    exit_checked_at: Optional[datetime] = None
    last_active_at: Optional[datetime] = None  # 上次活跃(用于错峰调度)
    write_paused_until: Optional[datetime] = None  # 平台风控后暂停自动写操作
    write_pause_reason: str = ""                    # 最近一次暂停原因
    identity_mode: str = "legacy"                  # legacy=保留存量画像 | native=浏览器原生画像
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MonitorTarget(SQLModel, table=True):
    """被监控的对象。抖音=用户;小红书=创作者(creator)或搜索关键词(keyword)。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)  # douyin | xhs
    target_kind: str = "creator"   # creator(账号/创作者) | keyword(小红书搜索词)
    keyword: str = ""              # target_kind=keyword 时的搜索词
    sec_uid: str = Field(default="", index=True)  # 抖音 sec_uid / 小红书 user_id
    xsec_token: str = ""          # 小红书:打开主页所需令牌(可选,缺失时靠登录态)
    nickname: str = ""
    avatar: str = ""
    alias: str = ""               # 管理别名,用于大量监控时快速识别
    group_name: str = Field(default="", index=True)  # 单一业务分组
    tags: str = ""                # JSON 字符串数组,用于多维筛选
    enabled: bool = True
    interval_seconds: int = 300
    initial_backfill_count: int = 0            # 首扫历史回填数;0=仅订阅后,-1=尽可能全量
    download_dir: str = ""                  # 自定义下载目录(空=用全局默认)
    video_quality: str = ""                 # 画质偏好(空=用全局默认)
    download_enabled: bool = True           # 新作品是否自动下载；关闭时仍保留作品记录
    media_filter: str = "all"               # all | video | images
    account_id: Optional[int] = None       # 用哪个登录账号的 Cookie 抓取
    last_scan_at: Optional[datetime] = None
    last_error: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ProxyPool(SQLModel, table=True):
    """代理池条目。提前配置好,账号从池里关联使用(一号一代理 sticky)。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    label: str = ""               # 备注名(如 "住宅-广东-01")
    url: str = Field(default="", index=True)  # http://user:pass@host:port / socks5://...
    enabled: bool = True          # 关闭后不参与自动分配
    status: str = "unknown"       # unknown | ok | bad(最近一次连通性测试结果)
    note: str = ""
    # 出口 IP 归属地(判别/测试时写入,用于核对 IP 地区是否与账号一致)
    exit_ip: str = ""
    country: str = ""
    region: str = ""
    city: str = ""
    isp: str = ""
    last_checked_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AppSetting(SQLModel, table=True):
    """全局键值设置(如默认下载目录)。"""
    key: str = Field(primary_key=True)
    value: str = ""


class AccountRiskState(SQLModel, table=True):
    """跨功能共享的账号平台风险状态，一账号一行。"""
    account_id: int = Field(primary_key=True)
    risk_level: int = 0
    cooldown_until: Optional[datetime] = Field(default=None, index=True)
    probe_only_until: Optional[datetime] = None
    consecutive_risk: int = 0
    consecutive_network_failures: int = 0
    network_failure_key: str = ""
    recovery_successes: int = 0
    last_risk_at: Optional[datetime] = None
    last_risk_reason: str = ""
    last_operation_at: Optional[datetime] = None
    last_write_at: Optional[datetime] = None
    last_heavy_read_at: Optional[datetime] = None
    last_recovery_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class RiskEvent(SQLModel, table=True):
    """统一平台操作计数事件；不保存 Cookie、代理凭据或响应正文。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True)
    network_key: str = Field(default="direct", index=True)
    operation_kind: str = Field(default="read_light", index=True)
    outcome: str = Field(default="success", index=True)
    signal: str = ""
    occurred_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class NotificationChannel(SQLModel, table=True):
    """通知渠道。对应逆向 model.NotificationChannel。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = ""
    type: str = "bark"             # bark | dingtalk | telegram
    config: str = ""              # JSON: 各渠道所需字段
    enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ContentRecord(SQLModel, table=True):
    """抓到的一条作品/笔记。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)  # douyin | xhs
    target_id: int = Field(index=True)
    aweme_id: str = Field(index=True)  # 抖音 aweme_id / 小红书 note_id
    desc: str = ""
    media_type: str = "video"          # video | images
    quality: str = ""                  # 实际下载的画质,如 1080p
    create_time: int = 0               # 作品发布时间(unix 秒)
    cover_url: str = ""
    like_count: int = 0                # 点赞数
    comment_count: int = 0             # 评论数
    duration: int = 0                  # 时长(秒)
    media_json: str = ""               # 媒体直链快照(JSON),用于失败重试
    xsec_token: str = ""               # 小红书:重新拉详情(feed)所需令牌
    download_status: str = "pending"   # pending | downloading | done | failed
    retry_count: int = 0               # 已重试次数
    local_path: str = ""
    error: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CommentWatch(SQLModel, table=True):
    """独立的评论监控对象(不依赖作品监控)。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)  # douyin | xhs
    kind: str = "video"            # video(单条视频/笔记) | user(账号/创作者近期作品)
    aweme_id: str = ""             # video 模式:被盯的视频 / 笔记 note_id
    sec_uid: str = ""             # user 模式:被盯的账号 / 创作者 user_id
    xsec_token: str = ""          # 小红书:打开笔记/主页所需的安全令牌(可能过期)
    title: str = ""               # 展示名(视频描述 / 账号昵称)
    avatar: str = ""
    alias: str = ""               # 管理别名
    group_name: str = Field(default="", index=True)  # 单一业务分组
    tags: str = ""                # JSON 字符串数组,用于多维筛选
    mode: str = "public"           # public(公开评论区) | creator(创作中心,仅抖音自有账号)
    account_id: Optional[int] = None
    interval_seconds: int = 600
    recent_works: int = 0          # 账号型监控检查最近 N 个作品；0=全局默认
    recent_days: int = 0           # 账号型监控只检查最近 N 天；0=全局默认
    max_scrolls: int = 0           # 单个评论区抓取深度；0=全局默认
    enabled: bool = True
    last_scan_at: Optional[datetime] = None
    last_error: str = ""
    comment_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PublishTask(SQLModel, table=True):
    """多平台发布任务(可定时、可来自跨平台作品转发)。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="xhs", index=True)   # xhs | douyin | kuaishou | shipinhao
    account_id: Optional[int] = None                   # 用哪个已登录账号发布
    media_type: str = "images"                         # images | video
    title: str = ""                                    # 标题(各平台上限不同)
    desc: str = ""                                     # 正文
    topics: str = ""                                   # 话题,逗号分隔(不带 #)
    location: str = ""                                 # 视频号:位置 POI(可选,best-effort)
    media_json: str = ""                               # 本地文件路径列表(JSON)
    visibility: str = "public"                         # 抖音:public 公开 | friends 好友可见 | private 仅自己可见
    allow_save: bool = True                            # 抖音:是否允许他人保存(下载)
    scheduled_at: Optional[datetime] = None            # 定时发布时间(空=尽快发)
    status: str = "pending"        # pending | publishing | uncertain | done | failed | canceled
    result_url: str = ""           # 发布成功后的笔记链接(能取到则填)
    error: str = ""
    source_platform: str = ""      # 来源(如 douyin),跨平台转发时填
    source_content_id: Optional[int] = None            # 来源作品记录 id
    created_at: datetime = Field(default_factory=datetime.utcnow)
    done_at: Optional[datetime] = None


class CommentRecord(SQLModel, table=True):
    """抓到的一条评论。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)  # douyin | xhs
    watch_id: Optional[int] = Field(default=None, index=True)   # 归属的评论监控
    target_id: int = Field(default=0, index=True)               # 旧:作品监控目标(兼容,0=无)
    aweme_id: str = Field(index=True)
    comment_id: str = Field(index=True)        # 抖音 cid
    text: str = ""
    user_nickname: str = ""
    like_count: int = 0
    create_time: int = 0                       # 评论时间(unix 秒)
    reply_to: str = ""                         # 上级评论 cid(子评论时)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class KeywordCollectionJob(SQLModel, table=True):
    """一次性的关键词批量采集任务。与持续轮询的 MonitorTarget 分开建模。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)  # douyin | xhs
    account_id: int = Field(index=True)
    keywords: str = "[]"                 # JSON 字符串数组
    max_contents_per_keyword: int = 20
    max_comments_per_content: int = 20
    include_replies: bool = False
    download_media: bool = False
    video_quality: str = "highest"
    download_dir: str = ""
    status: str = Field(default="pending", index=True)  # pending | running | done | partial | failed | canceled
    current_keyword: str = ""
    current_step: str = "等待执行"
    content_count: int = 0
    comment_count: int = 0
    error_count: int = 0
    error: str = ""
    cancel_requested: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class KeywordCollectionContent(SQLModel, table=True):
    """关键词任务发现的一条作品/笔记。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)
    platform: str = Field(default="douyin", index=True)
    keyword: str = Field(default="", index=True)
    aweme_id: str = Field(index=True)       # 抖音 aweme_id / 小红书 note_id
    desc: str = ""
    author_name: str = ""
    author_id: str = ""
    media_type: str = "video"
    create_time: int = 0
    cover_url: str = ""
    like_count: int = 0
    comment_count: int = 0                  # 平台显示的评论总数
    collected_comment_count: int = 0        # 本任务实际入库数
    media_json: str = "[]"
    xsec_token: str = ""
    download_status: str = "skipped"       # skipped | pending | downloading | done | failed
    local_path: str = ""
    error: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class KeywordCollectionComment(SQLModel, table=True):
    """关键词任务抓到的评论；按任务与作品单独保存，便于删除和导出。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)
    content_id: int = Field(index=True)
    platform: str = Field(default="douyin", index=True)
    aweme_id: str = Field(index=True)
    comment_id: str = Field(index=True)
    text: str = ""
    user_nickname: str = ""
    like_count: int = 0
    create_time: int = 0
    reply_to: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class DanmakuWatch(SQLModel, table=True):
    """短视频弹幕监控对象。弹幕与评论字段不同，单独建模。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)
    kind: str = "video"             # video(单条视频) | user(账号近期作品)
    aweme_id: str = Field(default="", index=True)
    sec_uid: str = Field(default="", index=True)
    title: str = ""
    avatar: str = ""
    alias: str = ""
    group_name: str = Field(default="", index=True)
    tags: str = "[]"
    mode: str = "public"            # public(播放页) | creator(创作中心)
    account_id: Optional[int] = None
    interval_seconds: int = 0       # 0=跟随全局 scan_interval_seconds
    recent_works: int = 0           # 0=跟随全局 danmaku_recent_works
    recent_days: int = 0            # 0=跟随全局 danmaku_recent_days
    max_scrolls: int = 0            # 0=跟随全局 danmaku_max_scrolls
    time_start_ms: int = 0          # 视频内时间起点,0=从头
    time_end_ms: int = 0            # 视频内时间终点,0=到结尾
    probe_step_seconds: float = 0.0  # 0=跟随全局时间轴步长
    include_keywords: str = "[]"    # 命中任一关键词才保留
    exclude_keywords: str = "[]"    # 命中任一关键词则丢弃
    min_text_length: int = 0
    max_text_length: int = 0
    min_like_count: int = 0
    max_records_per_scan: int = 0   # 0=跟随全局
    max_records_total: int = 0      # 0=跟随全局
    enabled: bool = True
    last_scan_at: Optional[datetime] = None
    last_error: str = ""
    danmaku_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DanmakuRecord(SQLModel, table=True):
    """抓到的一条短视频弹幕。video_time_ms 是视频内时间点。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)
    watch_id: Optional[int] = Field(default=None, index=True)
    aweme_id: str = Field(default="", index=True)
    danmaku_id: str = Field(default="", index=True)
    text: str = ""
    user_id: str = ""
    user_nickname: str = ""
    video_time_ms: int = 0
    create_time: int = 0
    like_count: int = 0
    is_blocked: bool = False
    source: str = "public"          # public | creator
    raw_json: str = "{}"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CommentRule(SQLModel, table=True):
    """自动评论规则(循环配置)。引擎按 interval 生成一批 CommentTask。
    auto_reply  = 回复「自己作品」收到的评论(内容目标较温和,但仍属于高敏感写操作)。
    auto_comment= 去「别人帖子」下评论(高风险,需节流+去重护着)。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)   # douyin | xhs
    name: str = ""                       # 备注名
    mode: str = "auto_reply"             # auto_reply | auto_comment
    account_id: Optional[int] = None     # 用哪个登录账号发(必填,发评论需登录态)
    # ── 目标范围 ──
    target_kind: str = "self"            # auto_reply: self(自己近期作品) | work(指定作品)
                                         # auto_comment: keyword(搜索词) | creator(指定博主)
    keyword: str = ""                    # target_kind=keyword 时的搜索词
    sec_uid: str = ""                    # creator 模式:目标博主 user_id / sec_uid
    aweme_id: str = ""                   # work 模式:指定作品 / 笔记 note_id
    xsec_token: str = ""                 # 小红书:打开目标所需令牌(可选)
    # ── 文案 ──
    templates: str = ""                  # JSON 字符串数组,支持 {nick} 变量与 spintax {a|b|c}
    use_ai: bool = False                 # 用大模型 API 生成文案(失败/未配置时回退模板库)
    reply_filter: str = ""               # auto_reply:仅回复正文含此关键词的评论(空=全回)
    skip_keywords: str = ""              # 命中任一(逗号分隔)则跳过该评论/作品
    # ── 节流 / 风控闸 ──
    daily_cap: int = 20                  # 该规则每日最多发多少条(每账号每日另有全局上限)
    min_gap_seconds: int = 90            # 同规则两条任务最小间隔(实际叠加 jitter)
    max_per_run: int = 5                 # 单轮最多生成多少条任务(避免一次铺太多)
    interval_seconds: int = 1800         # 规则多久跑一轮(发现+生成任务)
    require_review: bool = False         # 草稿审核:生成的任务为 draft,人工通过后才发
    enabled: bool = False                # 默认关,确认无误再开
    last_run_at: Optional[datetime] = None
    last_error: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CommentTask(SQLModel, table=True):
    """一条待发评论动作(由规则生成,或手动创建)。状态机同 PublishTask。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)   # douyin | xhs
    rule_id: Optional[int] = Field(default=None, index=True)   # 来源规则(手动=None)
    account_id: Optional[int] = None
    aweme_id: str = Field(default="", index=True)         # 目标作品 / 笔记 note_id
    xsec_token: str = ""                                  # 小红书:发评论所需令牌
    target_comment_id: str = ""                           # 非空=回复该条评论;空=作品下顶层评论
    target_nick: str = ""                                 # 被回复者昵称(供 {nick} 用)
    target_text: str = ""                                 # 目标评论原文(定位回复目标)
    content: str = ""                                     # 已渲染好的文案
    scheduled_at: Optional[datetime] = None               # 计划发送时间(错峰)
    # draft=草稿待审;uncertain=已提交但未取得明确结果,不可自动重试
    status: str = "pending"        # draft | pending | doing | uncertain | done | failed | canceled
    result: str = ""               # 成功后的评论 id / 链接
    error: str = ""
    method: str = ""               # 实际走的通道:manual | api | browser
    created_at: datetime = Field(default_factory=datetime.utcnow)
    done_at: Optional[datetime] = None


# ─────────── 本账号管理(作品 / 关注 / 粉丝 / 私信)───────────
class AccountWork(SQLModel, table=True):
    """本登录账号自己发布的一条作品(与监控别人的 ContentRecord 区分:这里挂在 account_id 上)。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)  # douyin | xhs | kuaishou
    account_id: int = Field(index=True)
    item_id: str = Field(default="", index=True)   # aweme_id / note_id / photo_id
    desc: str = ""
    media_type: str = "video"          # video | images
    cover_url: str = ""
    create_time: int = 0               # 发布时间(unix 秒)
    like_count: int = 0
    comment_count: int = 0
    collect_count: int = 0             # 收藏数
    share_count: int = 0
    play_count: int = 0                # 播放数(部分平台无)
    status: str = ""                   # 平台审核/可见状态(能取到则填)
    xsec_token: str = ""               # 小红书:打开笔记/抓评论所需令牌(其余平台空)
    raw_json: str = ""                 # 原始项快照
    fetched_at: Optional[datetime] = None  # 上次同步时间
    # ── 作品健康监控(B5)去重标记:同一异常只推一次 ──
    zero_play_alerted: bool = False    # 已就「发布满 N 小时仍 0 播」推过预警
    status_alerted: str = ""           # 已就此状态(如「违规/已删除」)推过预警(存已告警的状态文案)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AccountStatSnapshot(SQLModel, table=True):
    """本账号每日数据快照(B4:粉丝/作品/互动趋势)。每账号每天一行,体检时写入。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)
    account_id: int = Field(index=True)
    date: str = Field(default="", index=True)   # YYYY-MM-DD(账号时区);每账号每天唯一
    follower_count: int = 0
    aweme_count: int = 0
    total_like: int = 0                # 已同步本账号作品的点赞合计(能取到的范围)
    total_comment: int = 0
    total_play: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FollowEdge(SQLModel, table=True):
    """关注关系一行一人。direction=following(我关注的) / fan(关注我的)。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)  # douyin | xhs | kuaishou
    account_id: int = Field(index=True)
    direction: str = Field(default="following", index=True)  # following | fan
    uid: str = Field(default="", index=True)   # 对方 user_id(快手/小红书) / 抖音 uid
    sec_uid: str = ""                          # 抖音 sec_uid(用于打开主页/操作)
    nickname: str = ""
    avatar: str = ""
    signature: str = ""
    is_mutual: bool = False                    # 互关
    is_following: bool = True                   # 我当前是否已关注 ta(供回关/取关判断)
    raw_json: str = ""
    fetched_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DmConversation(SQLModel, table=True):
    """私信会话(一条会话 = 与某人的对话)。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)  # douyin | xhs | kuaishou
    account_id: int = Field(index=True)
    conv_id: str = Field(default="", index=True)   # 平台会话 id(无则用 peer_uid)
    peer_uid: str = ""
    peer_sec_uid: str = ""
    peer_nickname: str = ""
    peer_avatar: str = ""
    last_text: str = ""
    last_time: int = 0                  # 最近一条消息时间(unix 秒)
    unread_count: int = 0
    conv_short_id: str = ""             # 抖音 conversation_short_id(发消息用)
    ticket: str = ""                    # 抖音会话票据(发消息用)
    raw_json: str = ""
    fetched_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DmMessage(SQLModel, table=True):
    """私信单条消息。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)  # douyin | xhs | kuaishou
    account_id: int = Field(index=True)
    conv_id: str = Field(default="", index=True)
    msg_id: str = Field(default="", index=True)
    direction: str = "in"              # in(收到) | out(发出)
    msg_type: str = "text"            # text | image | ...
    text: str = ""
    create_time: int = 0
    raw_json: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AccountActionTask(SQLModel, table=True):
    """本账号写操作队列(取关/回关/发私信)。状态机同 CommentTask,带节流。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="douyin", index=True)  # douyin | xhs | kuaishou
    account_id: int = Field(index=True)
    action: str = Field(default="follow", index=True)  # follow | unfollow | send_dm
    target_uid: str = ""              # 目标 user_id
    target_sec_uid: str = ""          # 抖音 sec_uid
    target_nick: str = ""             # 展示用
    conv_id: str = ""                 # send_dm:会话 id(可空,用 target_uid 新开)
    content: str = ""                 # send_dm 的文案
    scheduled_at: Optional[datetime] = None
    status: str = "pending"        # draft | pending | doing | done | failed | uncertain | canceled
    result: str = ""
    error: str = ""
    method: str = ""               # 实际走的通道:browser
    min_gap_seconds: int = 60      # 同账号两次写操作最小间隔
    created_at: datetime = Field(default_factory=datetime.utcnow)
    done_at: Optional[datetime] = None


class ShareDownloadRecord(SQLModel, table=True):
    """分享链接下载历史。只读作品信息不会写入，实际下载成功或失败都会记录。"""
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="generic", index=True)
    source_url: str = ""
    account_id: Optional[int] = Field(default=None, index=True)
    item_id: str = Field(default="", index=True)
    title: str = ""
    author: str = ""
    media_type: str = ""
    media_count: int = 0
    cover_url: str = ""
    status: str = Field(default="done", index=True)  # done | failed
    output_dir: str = ""
    files_json: str = "[]"
    metadata_json: str = "{}"
    error: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
