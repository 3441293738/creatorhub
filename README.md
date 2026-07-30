# CreatorHub

> 本地运行的多平台内容管理面板，支持 **抖音 / 小红书 / 快手 / 视频号**。

CreatorHub 使用 Python + FastAPI 提供统一 Web 界面，用于管理账号、监控作品与评论、下载内容、发布作品和接收通知。账号登录态、数据库及媒体文件均保存在本地。

## 平台能力

| 功能 | 抖音 | 小红书 | 快手 | 视频号 |
|---|:---:|:---:|:---:|:---:|
| 登录 | 扫码 / 创作者 / Cookie | 扫码 | 扫码 / 创作者 | 扫码 |
| 作品监控 | ✅ | ✅ 创作者 / 关键词 | ✅ | 仅本账号 |
| 评论监控 | ✅ | ✅ | ✅ | 仅本账号 |
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
- Node.js 18+（仅小红书发布需要）
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
python -m playwright install chromium

# 复制 config.example.yaml 为 config.yaml 后启动
python selftest.py
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

使用小红书发布功能时，还需安装 Node.js 依赖：

```bash
npm install
```

</details>

## 界面预览

### 总览

![总览面板](assets/screenshots/overview-douyin.png)

### 账号与代理

![账号登录与代理池](assets/screenshots/accounts-proxy.png)

### 作品监控

![作品监控与下载](assets/screenshots/monitor-posts.png)

### 评论监控

![评论监控](assets/screenshots/monitor-comments.png)

## 基本使用

### 1. 添加账号

1. 在顶部选择平台。
2. 打开左侧「账号」。
3. 选择扫码、创作者登录或 Cookie 登录。
4. 登录完成后，可在账号列表中刷新资料、检测状态或重新登录。

小红书扫码会依次获取读取态和创作态；需要发布时，请在跳转到创作平台后完成登录再关闭窗口。添加笔记或创作者监控时，建议使用包含 `xsec_token` 的完整链接。

### 2. 监控与下载

- **作品监控**：添加创作者主页、作品链接、短链或平台 ID，发现新作品后自动入库。
- **评论监控**：可订阅单条作品，也可监控账号近期作品的评论。
- **链接下载**：粘贴完整分享文案或链接，自动提取地址并下载。
- **历史内容**：新增目标默认只监控订阅后的作品，也可选择回填最近若干条。

下载支持断点续传和失败重试；抖音可选画质，小红书支持图集和视频。

### 3. 发布与转发

- 小红书支持图集、视频和定时发布。
- 抖音、快手和视频号通过对应创作平台发布。
- 已下载的抖音作品可转发到小红书或视频号，小红书作品可转发到抖音；发布前可修改标题、正文和话题。

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

## 常见问题

| 问题 | 处理方式 |
|---|---|
| Playwright 启动失败或找不到浏览器 | 运行 `python -m playwright install chromium` |
| 扫码登录没有弹窗 | 确认当前机器有桌面环境；抖音也可使用 Cookie 登录 |
| Windows 下出现 Playwright 子进程错误 | 使用单 worker 启动，不要添加 `--workers` |
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

## 赞助商

<p align="center">
  <a href="https://www.ipwo.net/?code=PPBFE3E2F" target="_blank" rel="noopener noreferrer">
    <img src="assets/sponsors/ipwo-banner.svg" alt="IPWO 爬虫住宅代理" width="100%">
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
