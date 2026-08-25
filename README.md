# CreatorHub

> 本地运行的多平台内容管理面板，支持 **抖音 / 小红书 / 快手 / 视频号**。

[在线预览](https://3441293738.github.io/creatorhub/) · [快速开始](#快速开始) · [平台能力](#平台能力) · [交流群](#交流群)

> 在线预览由 GitHub Pages 提供，使用脱敏示例数据，仅展示界面与交互；登录、抓取、下载和发布仍需在本地运行。

CreatorHub 使用 Python + FastAPI 提供统一 Web 界面，用于管理账号、监控作品与评论、下载内容、发布作品和接收通知。账号登录态、数据库及媒体文件均保存在本地。

浏览器自动化由免费开源的 [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) 驱动；业务层继续使用兼容的 Playwright API，现有账号 Profile 和登录态目录结构保持不变。

## 平台能力

| 功能 | 抖音 | 小红书 | 快手 | 视频号 |
|---|:---:|:---:|:---:|:---:|
| 登录 | 扫码 / 创作者 / Cookie | 扫码 | 扫码 / 创作者 | 扫码 |
| 关键词批量采集 | ✅ 作品 / 评论 / 媒体 | 下版本 | — | — |
| 作品监控 | ✅ | ✅ 创作者 / 关键词 | ✅ | 仅本账号 |
| 评论监控 | ✅ | ✅ | ✅ | 仅本账号 |
| 短视频弹幕监控 | ✅ 播放页 / 创作中心 | — | — | — |
| 内容下载 | ✅ 可选画质 | ✅ 图集 / 视频 | ✅ | — |
| 发布 | ✅ | ✅ | ✅ | ✅ |
| 自动评论 / 回复 | ✅ | ✅ | ✅ | — |
| 本账号管理 | 作品 / 关注 / 粉丝 / 私信 | 作品 / 关注 / 粉丝 / 私信 | 作品 / 关注 / 粉丝 | 作品 / 数据 / 评论 |
| 通知 | Bark / 钉钉 / Telegram | Bark / 钉钉 / Telegram | Bark / 钉钉 / Telegram | Bark / 钉钉 / Telegram |

> 视频号只支持创作者助手中的本账号数据，不支持监控或下载他人作品。

## 快速开始

### 环境要求

- Python 3.10+
- 桌面环境（扫码登录时需要弹出浏览器）
- Google Chrome 稳定版（可选，但小红书扫码登录建议安装）
- Node.js 18+（仅启用小红书 `api` 发布兼容模式时需要）
- 系统 ffmpeg（可选；未安装时自动使用 Python 依赖附带的 ffmpeg）

### 一键启动

克隆项目：

```bash
git clone https://github.com/3441293738/creatorhub.git
cd creatorhub
```

Windows：

```bat
.\start.cmd
```

macOS / Linux：

```bash
chmod +x start.sh
./start.sh
```

首次运行会自动创建虚拟环境、安装依赖和 Chromium、生成 `config.yaml`，随后打开：

```text
http://127.0.0.1:8000
```

> **小红书登录建议：** 尽量使用本机系统中已安装的稳定版 Google Chrome。CreatorHub 会优先通过 CDP 启动系统 Chrome，并为每个账号使用独立的持久化 Profile，不会读取或复用个人 Chrome 的日常 Profile；未安装 Chrome 时会自动回退到可见的 Patchright Chromium。

常用命令：

```bash
.\start.cmd install        # 重新安装或更新依赖
.\start.cmd check          # 环境自检
.\start.cmd --no-open      # 启动后不自动打开页面
.\start.cmd --port 8080    # 使用其他端口
.\start.cmd --reload       # 开发模式
```

> macOS / Linux 将 `.\start.cmd` 换成 `./start.sh`。

<details>
<summary>手动安装</summary>

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

python -m pip install -r requirements.txt
python -m patchright install chromium

# 复制 config.example.yaml 为 config.yaml 后启动
python selftest.py
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

仅当显式启用小红书 API 发布兼容模式时，才需安装 Node.js 依赖：

```bash
npm install
```

</details>

## 界面预览

> 截图使用脱敏示例数据；界面配色会跟随当前平台切换。

![总览面板](assets/screenshots/overview-douyin.png)

### 更多界面

> 截图统一为 1600 × 1000，点击可查看高清原图。

<table>
  <tr>
    <td width="50%" align="center" valign="top"><strong>小红书浅色主题</strong><br><a href="assets/screenshots/overview-xiaohongshu.png"><img src="assets/screenshots/overview-xiaohongshu.png" alt="小红书总览面板" width="100%"></a></td>
    <td width="50%" align="center" valign="top"><strong>账号与代理</strong><br><a href="assets/screenshots/accounts-proxy.png"><img src="assets/screenshots/accounts-proxy.png" alt="账号登录与代理池" width="100%"></a></td>
  </tr>
  <tr>
    <td width="50%" align="center" valign="top"><strong>作品监控</strong><br><a href="assets/screenshots/monitor-posts.png"><img src="assets/screenshots/monitor-posts.png" alt="作品监控与下载" width="100%"></a></td>
    <td width="50%" align="center" valign="top"><strong>评论监控</strong><br><a href="assets/screenshots/monitor-comments.png"><img src="assets/screenshots/monitor-comments.png" alt="评论监控" width="100%"></a></td>
  </tr>
  <tr>
    <td width="50%" align="center" valign="top"><strong>内容发布</strong><br><a href="assets/screenshots/publish-workflow.png"><img src="assets/screenshots/publish-workflow.png" alt="内容发布与任务队列" width="100%"></a></td>
    <td width="50%" align="center" valign="top"><strong>链接下载</strong><br><a href="assets/screenshots/share-download.png"><img src="assets/screenshots/share-download.png" alt="分享链接解析与下载历史" width="100%"></a></td>
  </tr>
  <tr>
    <td width="50%" align="center" valign="top"><strong>自动评论</strong><br><a href="assets/screenshots/autocomment-rules.png"><img src="assets/screenshots/autocomment-rules.png" alt="自动评论规则与任务记录" width="100%"></a></td>
    <td width="50%" align="center" valign="top"><strong>本账号管理与私信</strong><br><a href="assets/screenshots/account-hub-dm.png"><img src="assets/screenshots/account-hub-dm.png" alt="本账号数据与私信管理" width="100%"></a></td>
  </tr>
</table>

## 基本使用

### 1. 添加账号

1. 在顶部选择平台。
2. 打开左侧「账号」。
3. 选择扫码、创作者登录或 Cookie 登录。
4. 登录完成后，可在账号列表中刷新资料、检测状态或重新登录。

小红书扫码登录只获取主站读取态；需要发布时，请再使用独立的「创作者登录」入口。添加笔记或创作者监控时，建议使用包含 `xsec_token` 的完整链接。

### 2. 监控与下载

- **关键词批量采集**：当前版本先支持抖音；一次输入最多 20 个关键词，设置每词作品数、每作品评论数、是否包含二级评论及是否下载媒体；已结束任务支持编辑配置、保留结果去重续跑和 Excel 导出。小红书关键词采集安排在下个版本。
- **作品监控**：添加创作者主页、作品链接、短链或平台 ID，发现新作品后自动入库。
- **评论监控**：可订阅单条作品，也可监控账号近期作品的评论。
- **短视频弹幕监控**：独立于评论区，按视频内时间轴渐进探测并排序；支持时间范围、关键词、文本长度、点赞数和容量上限过滤，记录持久化到 SQLite；自己的视频走创作中心，公开视频走播放器拦截。
- **链接下载**：粘贴完整分享文案或链接，自动提取地址并下载。
- **历史内容**：新增目标默认只监控订阅后的作品，也可选择回填最近若干条。

下载支持断点续传和失败重试；抖音可选画质，小红书支持图集和视频。

关键词采集是一次性批处理，与持续轮询的作品监控相互独立。搜索结果受平台排序、账号登录态和当前可见范围影响，因此作品数和评论数是采集上限，不代表平台全量数据。

### 3. 发布与转发

- 小红书支持图集、视频和定时发布。
- 抖音、快手和视频号通过对应创作平台发布。
- 已下载的抖音作品可转发到小红书或视频号，小红书作品可转发到抖音；发布前可修改标题、正文和话题。

### 小红书独立 Chrome CDP 模式

- 默认 `xhs_browser_mode: auto`：每个小红书账号启动一个独立、可见的系统 Chrome CDP 会话，并长期复用该账号自己的 Profile、Cookie、缓存和本地存储。
- 项目专用 Profile 与用户平时打开 Chrome 使用的默认 Profile 完全分开；请勿同时用其他 Chrome 进程打开项目的账号 Profile。
- 页面任务加载完整的图片、字体和媒体资源；搜索、滚动、输入、发布和评论统一走可见页面控件，且整台机器同一时刻只执行一个小红书可见操作。
- 同一轮搜索与笔记详情复用账号现有浏览器进程和 Profile，不再每条笔记重新启动内核；页面停留时间会按可见内容长度做有界抖动，滚动、输入、详情间隔和阶段性休息采用有限随机节奏。默认单轮详情量也会在较小范围内变化，显式配置“单轮上限”时仍严格按用户上限执行。
- 机器未安装稳定版 Chrome 时，`auto` 会回退到可见的 Patchright Chromium；账号列表会显示实际后端和回退原因。`cdp` 为严格模式，Chrome 缺失或 Profile 冲突时直接报错。
- 账号代理支持 HTTP、HTTPS、SOCKS5 及账号密码认证。代理不可连接或认证失败时按失败关闭处理，不会静默改走本机直连；建议同一账号长期保持稳定出口。
- 小红书发布和评论默认使用 `browser` 页面模式。提交按钮只点击一次；提交后若浏览器连接中断或缺少成功证据，任务会标记为“结果待确认”，不会自动重试，需先到平台核对。
- 如需绕过系统 Chrome CDP，可显式设置 `xhs_browser_mode: patchright`。旧配置值 `playwright` 会自动迁移为 `patchright`。

### 可选：开源 Fingerprint Chromium 内核

CreatorHub 可以把开源的
[`fingerprint-chromium`](https://github.com/adryfish/fingerprint-chromium)
作为可插拔 Chromium 运行时。账号、Profile、Cookie、代理、LRU 和风控仍由
CreatorHub 管理，不需要外部商业浏览器或云端账号。

1. 从上游 Release 下载适合当前系统的构建并自行校验文件。
2. 在 `config.yaml` 配置浏览器路径：

```yaml
engine:
  browser_backend: local
  fingerprint_chromium_path: D:/browsers/fingerprint-chromium/chrome.exe
  fingerprint_chromium_allow_headless: false
  fingerprint_chromium_platform: auto
```

3. 重启 CreatorHub，在「账号」列表点击「环境」，选择
   `Fingerprint Chromium · 开源内核`。`browser_backend: fingerprint_chromium`
   可以将它设为所有未单独指定账号的全局默认值。

添加新账号时，选择具体 Fingerprint Chromium 内核和代理后，会在浏览器首次启动前
打开「登录前指纹配置」。语言、时区、位置和窗口可继续跟随出口 IP 自动生成，也可切换
为自定义；操作系统、浏览器品牌、CPU、WebGL、Canvas、WebRTC 和附加参数同样可编辑。
登录成功后，这套配置与该账号的独立 Profile 一起持久化，后续仍可通过账号行的「指纹」
按钮修改。

该后端使用账号现有 `fp_seed` 生成稳定的 32 位内核指纹种子，并由浏览器内核
统一处理 UA/Client Hints、Canvas、Audio、WebGL、语言和时区；CreatorHub 不会
再叠加 legacy JavaScript 指纹脚本。默认强制使用有头窗口，因为上游说明无头模式
只处理了部分无头特征。切换已有账号的浏览器环境会改变其设备画像，建议切换后重新
检查登录态和代理出口。小红书账号选择该后端后会直接启动该 Chromium，不再进入系统
Chrome CDP 分支。

每个新的指纹 Profile（以及内核、代理或指纹配置变化后的新环境）首次进入可见登录/
账号浏览器时，会额外打开 `https://www.browserscan.net/zh` 体检标签，供用户核对实际
IP、时区、WebRTC 与浏览器指纹。账号页的「环境检测」可以随时重新打开；页面显示的
“已提示”只表示检测页已打开，不代表目标平台风控一定通过。BrowserScan 是第三方站点，
会看到该环境的出口 IP 和浏览器特征。

项目不捆绑上游浏览器二进制；升级浏览器时请先备份 `data/profiles/` 并在测试账号上
验证兼容性。

### 4. 本账号与通知

- 「本账号」中可同步自己的作品、关注、粉丝和私信，具体能力因平台而异。
- 可启用作品健康监控，对零播放、违规或下架状态发送提醒。
- 通知渠道支持 Bark、钉钉和 Telegram。

> 自动评论、回复、私信及关注操作受平台风控影响，建议低频使用。

## 配置

首次启动会从 `config.example.yaml` 生成 `config.yaml`。大部分常用选项也可以在 Web 面板的「设置」中修改。

```yaml
engine:
  scan_interval_seconds: 300         # 默认轮询间隔
  monitor_initial_backfill_count: 0  # 0=只监控新作品，-1=尽可能回填
  worker_pool_size: 2                # 下载并发数
  scan_concurrency: 2                # 抓取并发数
  account_check_interval_seconds: 1800
  media_dir: ./data/media
  work_health_enabled: false

storage:
  db_path: ./data/creatorhub.db

proxies: []
  # - http://user:pass@host:port
  # - socks5://user:pass@host:port
```

完整配置及说明见 [`config.example.yaml`](config.example.yaml)。

- `config.yaml`、数据库、登录态和媒体文件默认不会提交到 Git。
- 每个账号使用独立浏览器配置目录；如使用代理，建议为账号绑定稳定的独立代理。
- 数据库字段会在启动时自动迁移，升级后通常无需删除旧数据库。

## 分享链接命令行下载

只解析链接，不访问网络：

```bash
python -m app.engine.share_downloader --links-only "完整分享文案或链接"
```

下载内容：

```bash
python -m app.engine.share_downloader "完整分享文案或链接" -o ./data/media/share -q 1080
```

在 Web 面板中也可以直接使用「链接下载」。

## 统一平台风控

默认启用 `risk_control.mode: conservative`。它不会删除或关闭现有的登录、监控、下载、发布、评论、关注、取关和私信功能，而是把平台操作统一送入持久化闸门：

- 同一账号的评论、账号动作、私信和发布共享写操作间隔与额度，立即执行也不会绕过冷却。
- 使用同一网络出口的账号串行访问；未配置代理的账号统一归入 `direct` 出口组。
- `conservative` 模式中的写间隔、小时/每日额度和同出口并发是不可放宽的保护线；配置 `0` 表示沿用保护线，而不是取消限制。切换为 `custom` 后才完全按自定义值执行。
- 轻读取与重读取分别计时，评论区、弹幕、作品健康等重读取默认至少间隔 60 秒。
- 命中 `403/429/461/471`、验证码或明确风控提示后，依次冷却 30 分钟、2 小时、6 小时和 24 小时。
- 冷却结束后只放行间隔式轻量探测；连续 3 次成功才降低一级风险状态。
- 被额度、时段、代理状态或冷却拦下的任务仍保持 `pending`，服务重启也会恢复中断的执行态。
- 登录态失效时任务保留并等待重新登录；同一代理连续两次连接失败后会将账号和代理池条目标记为不可用。
- 存量账号继续使用 `legacy` 浏览器画像，避免已有 profile 漂移；新扫码与 Cookie 账号使用 `native` 模式，不再注入自定义 UA、Client Hints、定位和指纹脚本。
- `native` 账号的发布、评论、关注和私信统一经过写环境硬门禁：系统 Chrome、有头页面、独立 Profile，以及已配代理的浏览器出口基线必须全部正常。
- 在账号页执行“测试代理”时，`native` 账号会由自身 BrowserContext 记录 IP/国家/ASN/时区；出口漂移或基线过期后写任务保留在队列，重新验证后再执行。
- 每个账号 Profile 都有跨进程占用保护：Patchright 使用 `.browser.lock`，系统 Chrome CDP 使用带 PID/启动参数验证的 owner marker，防止多进程同时打开相同目录。同一出口下多个 `native` 账号在短时间内同时命中风险时，会触发出口组熔断。

Web 面板的「风控中心」集中展示正常、冷却、渐进恢复、登录失效、代理异常和网络熔断账号，并提供触发原因、冷却截止、恢复进度、任务级受阻原因、网络出口及事件时间线。规则页可在线调整读取间隔、冷却阶梯、恢复探测、写操作额度、出口组熔断和活跃时段；配置保存到本地数据库并立即作用于后续调度，重启后会自动恢复。冷却结束后，独立恢复调度器会按探测间隔主动验证账号，不依赖闲置保活周期；恢复完成后自动唤醒明确受风控阻塞的任务。人工解除必须填写原因，并与规则修改、人工探测一起写入审计记录。

服务暴露到局域网时，建议设置环境变量 `CREATORHUB_ADMIN_TOKEN`。设置后，风控规则保存、人工探测和解除接口必须携带管理口令；Web 风控中心可通过“设置管理口令”仅在当前浏览器会话中保存它。

完整参数及保守默认值见 [`config.example.yaml`](config.example.yaml) 的 `risk_control` 段。

## 常见问题

| 问题 | 处理方式 |
|---|---|
| macOS 安装依赖时报 `command /usr/bin/clang++ failed with code 1` | 更新代码后删除旧的 `.venv`，再运行 `./start.sh install`；安装器会先升级 pip/setuptools/wheel。 |
| Patchright 启动失败或找不到浏览器 | 运行 `python -m patchright install chromium` |
| 扫码登录没有弹窗 | 确认当前机器有桌面环境；抖音也可使用 Cookie 登录 |
| 小红书扫码登录出现设备安全验证 | 尽量安装或更新本机稳定版 Google Chrome，并保持同一账号的 Profile 和网络出口稳定；没有 Chrome 时项目会回退到 Patchright Chromium |
| Windows 下出现 Patchright 子进程错误 | 使用单 worker 启动，不要添加 `--workers` |
| 抓取不到作品或评论 | 检查登录态、目标链接和网络状态，必要时重新登录并降低频率 |
| 小红书链接解析失败 | 重新复制包含有效 `xsec_token` 的完整链接 |
| 仅音频仍得到 MP4，或视频没有声音/画质受限 | 重新运行安装命令更新依赖；也可安装系统 ffmpeg 并加入 `PATH` |

仍有问题可提交 [Issue](https://github.com/3441293738/creatorhub/issues)，并附上平台、操作步骤和服务端错误日志。

## 数据目录

```text
data/
├─ creatorhub.db   # SQLite 数据库
├─ media/          # 下载内容
└─ profiles/       # 账号浏览器配置与登录态
```

备份项目前，建议一并备份 `config.yaml` 和 `data/`。

## 交流群

欢迎加入 **CreatorHub 交流群**，交流使用经验、问题反馈和功能建议。

<p align="center">
  <a href="https://3441293738.github.io/creatorhub/community/">
    <img src="assets/community/live-entry.png" alt="CreatorHub 交流群固定入口二维码" width="280">
  </a>
</p>

<p align="center">
  扫码或点击二维码打开<a href="https://3441293738.github.io/creatorhub/community/">交流群固定入口</a>；微信群二维码到期后会在入口页更新。
</p>

## 赞助商

<p align="center">
  <a href="https://www.ipwo.net/?code=PPBFE3E2F" target="_blank" rel="noopener noreferrer">
    <img src="assets/sponsors/ipwo-banner.png" alt="IPWO 爬虫住宅代理" width="100%">
  </a>
</p>

<p align="center">
  <a href="https://www.ipwo.net/?code=PPBFE3E2F" target="_blank" rel="noopener noreferrer">IPWO</a>
  提供稳定的住宅代理网络，适用于公开数据采集、接口调试、自动化测试与多地区访问验证等合规场景。
  支持 HTTP / HTTPS / SOCKS5，优惠码：<code>0201</code>。
  <br>
  请在合法授权并遵守目标站点条款的前提下使用。
</p>

## 友链

- [LINUX DO](https://linux.do/) — 感谢社区提供的帮助与支持。

## 使用说明

本项目用于技术学习和个人内容管理，不提供账号、Cookie、代理或平台数据。使用时请遵守目标平台规则及所在地法律法规，并尊重内容版权和个人隐私。
