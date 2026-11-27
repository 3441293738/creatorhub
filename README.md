# CreatorHub

本地运行的多平台内容管理工作台，支持 **抖音 / 小红书 / 海外小红书 / 快手 / 视频号 / TikTok / 哔哩哔哩 / Instagram / Twitter / Facebook / YouTube**。

[在线预览](https://3441293738.github.io/creatorhub/) · [平台能力](#平台能力) · [使用指南](https://3441293738.github.io/creatorhub/guide/) · [下载安装](https://github.com/3441293738/creatorhub/releases/latest) · [问题反馈](https://github.com/3441293738/creatorhub/issues) · [赞赏与交流](#赞赏与交流) · [授权说明](#授权说明)

<p align="center">
  <a href="https://3441293738.github.io/creatorhub/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/hero-banner-dark.svg">
      <img alt="CreatorHub 本地多平台内容管理工作台动态架构预览" src="assets/hero-banner.svg" width="100%">
    </picture>
  </a>
</p>

统一管理多平台账号矩阵、汇总预览全站内容、采集与监控动态、下载素材并执行跨平台发布与互动任务。账号登录态、数据库与媒体文件保存在本地，各平台能力以[使用指南](https://3441293738.github.io/creatorhub/guide/#welcome)为准。

> 在线预览使用脱敏示例数据，仅展示界面与交互；真实登录、采集、下载和发布需要在本地运行。

## 核心矩阵与汇总能力

- **全平台指挥概览**：支持「全平台」全局视角与单平台独立视角一键切换，集中呈现跨平台粉丝规模、异常待办快筛（登录失效、代理故障、共用代理/直连提醒、风控冷却、未读私信、异常作品）、十一平台健康态势卡、近期捕获高赞作品与发布排期。
- **四源合一汇总预览库**：打通主页作品监控、关键词采集、自有账号作品与分享链接下载四类内容源，提供可视化画廊与右侧分栏实时快览（直接播放本地视频、翻阅多图图集、查看高赞评论，支持 `J` / `K` 快捷键切换与一键带入群发）。
- **跨平台账号矩阵运维**：支持按人设或业务创建跨平台矩阵分组、标记账号角色（创作主号 / 采集小号 / 混合）与置顶别名；提供分组矩阵、密集运维总表、活跃浏览器运行墙三种视图，支持批量均衡分配代理、一键切换本机直连、切换浏览器内核、顺序探活及失效账号连续扫码补登向导。
- **矩阵数据对比与聚合私信**：跨账号横向对比粉丝增减、播放与获赞表现，集中巡检 0 播与状态异常作品，并在同一收件箱聚合多平台未读私信会话；切换具体平台后可进入单账号精细管理。
- **多账号错峰矩阵群发**：上传一次视频或图文素材与主文案，按矩阵组一键勾选多平台目标账号，自动校验各平台标题字数上限、支持分平台单独覆盖标题与可见范围，并按设定间隔自动错峰入队发布。
- **账号级防关联隔离与本地极速缓存**：十一大平台按账号严格隔离持久化 Profile、硬件与语言指纹、TLS 握手指纹及代理出口（内置一体化登录环境向导、结构化代理选择器与多维代理池管理，支持本机直连、同区节点均衡复用与跨区防错配保护，对同平台共用 IP 或直连场景提供非阻断式防关联提醒，自动启用远程 DNS `socks5h` 与 WebRTC 防旁路泄露），全流程内置贝塞尔轨迹与作息节律拟人引擎；采集与监控封面、头像及媒体自动本地持久化缓存，避免重复动态加载与外链过期。

## 平台能力

| 平台 | 主要功能 |
|---|---|
| 抖音 | 主页作品 / 评论 / 弹幕监控、关键词采集、下载、发布与私信管理 |
| 小红书 | 主页笔记 / 评论监控、关键词采集、下载、发布与私信管理 |
| 海外小红书 | 四域名隔离（`rednote.com`）、关键词采集、主页作品与只读评论监控、下载、本账号作品管理与图文视频发布 |
| 快手 | 主页作品 / 评论监控、关键词采集、下载与发布 |
| 视频号 | 本账号作品、数据与发布；连接本机微信后支持关键词采集、主页作品 / 评论监控与加密视频下载 |
| TikTok | 关键词视频采集、主页作品 / 评论监控、下载、视频发布与用户互动 |
| 哔哩哔哩 | 关键词采集、UP 主空间视频与动态监控、评论 / XML 弹幕监控、下载、分区投稿 / 图文动态发布与私信管理 |
| Instagram | 关键词 / 话题采集、主页作品与评论监控、轮播与 Reels 下载、单图 / 轮播 / Reels / Story 发布与 Direct 私信管理 |
| Twitter | 关键词推文采集、主页作品与回复监控、高清原图与最高码率视频下载、纯文本 / 最多 4 图 / 单视频发推（含回复权限与首评串推）、转推 / 引用与私信管理 |
| Facebook | SSR + 动态 GraphQL 协议直采与隔离浏览器回退、关键词动态采集、个人/公共主页/群组作品与评论监控、帖子/Reels/Watch 媒体下载、多目标发布（个人动态 / Profile+ 公共主页 `i_user` 切换 / 群组 / Story 与首评）、7 种表情赞/分享/加群/加好友与 Messenger 私信管理 |
| YouTube | SSR `ytInitialData` + Innertube Web (`/youtubei/v1/*`) + `SAPISIDHASH` 协议直采与隔离浏览器回退、关键词筛选采集、频道视频与 Shorts 主页监控及评论监控、`yt-dlp` + FFmpeg 无损合流下载、YouTube Studio 视频/Shorts 上传（多频道切换/标签/播单/缩略图/首映/首评）、点赞/点踩/订阅/评论互动与真实观看养号 |

> 视频号读取他人内容和下载视频需要先在「设置 → 视频号微信通道」连接本机已登录的桌面微信；TikTok 当前支持单视频发布，暂不支持图文发布与直播。详细能力、操作步骤和使用限制见[使用指南](https://3441293738.github.io/creatorhub/guide/#welcome)。

## 快速开始

### Windows 安装版（推荐）

1. 在 [Releases](https://github.com/3441293738/creatorhub/releases/latest) 的 Assets 中下载 `CreatorHub-Setup-版本-windows-x64.exe`。
2. 双击运行安装向导，可保持默认位置或自由选择其他磁盘目录（如 `D:\CreatorHub`）安装，完成后通过快捷方式启动。
3. 新版启动后自动准备工作台，打开面板并登录账号即可使用。

适用于 Windows 10/11 x64，无需手动安装 Python；安装与首次启动请保持联网。版本更新见[更新与备份](https://3441293738.github.io/creatorhub/guide/#backup)。

### 源码运行（开发者 / macOS / Linux）

准备 Python 3.10+ 和桌面环境，其他依赖与平台要求见[安装指南](https://3441293738.github.io/creatorhub/guide/#install)。

```bash
git clone https://github.com/3441293738/creatorhub.git
cd creatorhub
```

Windows：

```powershell
.\start.cmd
```

macOS / Linux：

```bash
chmod +x start.sh
./start.sh
```

首次运行自动准备虚拟环境、Python 依赖、浏览器组件和 `config.yaml`，随后打开 `http://127.0.0.1:8000`。

## 使用文档

详细功能、平台差异、操作步骤和配置统一维护在 **[使用指南](https://3441293738.github.io/creatorhub/guide/)**：

- 按平台上手：[抖音](https://3441293738.github.io/creatorhub/guide/douyin/) · [小红书](https://3441293738.github.io/creatorhub/guide/xhs/) · [海外小红书](https://3441293738.github.io/creatorhub/guide/rednote/) · [快手](https://3441293738.github.io/creatorhub/guide/kuaishou/) · [视频号](https://3441293738.github.io/creatorhub/guide/shipinhao/) · [TikTok](https://3441293738.github.io/creatorhub/guide/tiktok/) · [哔哩哔哩](https://3441293738.github.io/creatorhub/guide/bilibili/) · [Instagram](https://3441293738.github.io/creatorhub/guide/instagram/) · [Twitter](https://3441293738.github.io/creatorhub/guide/twitter/) · [Facebook](https://3441293738.github.io/creatorhub/guide/facebook/) · [YouTube](https://3441293738.github.io/creatorhub/guide/youtube/)
- 日常使用：[全平台矩阵与汇总预览](https://3441293738.github.io/creatorhub/guide/#matrix) · [功能手册](https://3441293738.github.io/creatorhub/guide/#monitor) · [配置与账号环境](https://3441293738.github.io/creatorhub/guide/#settings) · [更新与备份](https://3441293738.github.io/creatorhub/guide/#backup)
- 开发参考：[配置示例](config.example.yaml) · [更新日志](CHANGELOG.md) · [文档维护](guide/README.md)

## 技术栈

基于 Python、FastAPI、SQLite 与兼容 Playwright 的浏览器自动化接口。浏览器按平台使用系统 Chrome/CDP 或 [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)。

## Star History

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/3441293738/creatorhub/star-history/assets/star-history-dark.svg">
  <img alt="CreatorHub Star History Chart" src="https://raw.githubusercontent.com/3441293738/creatorhub/star-history/assets/star-history.svg">
</picture>

## 赞助商

<p align="center">
  <a href="https://www.ipwo.net/?code=PPBFE3E2F" target="_blank" rel="noopener noreferrer">
    <img src="assets/sponsors/ipwo-banner.jpg" alt="IPWO 住宅代理：195+ 地区，多协议适配，动态 / 静态 / 不限量代理" width="100%">
  </a>
</p>

<p align="center">
  <a href="https://www.ipwo.net/?code=PPBFE3E2F" target="_blank" rel="noopener noreferrer">IPWO</a>
  住宅代理资源，适用于爬虫开发、数据研究、市场分析等场景，为开发者探索更多数据应用方向提供支持。
  <br>
  覆盖 195+ 地区，包含动态代理 / 静态代理 / 不限量代理，支持 HTTP / HTTPS / SOCKS5 多种协议。
  可免费试用，优惠折扣码：<code>0201</code>。
</p>

## 赞赏与交流

如果 CreatorHub 对你有帮助，欢迎自愿赞赏，支持项目持续维护与改进。

欢迎加入 **CreatorHub 交流群**，交流使用经验、问题反馈和功能建议。

<table>
  <tr>
    <th align="center" width="25%">赞赏支持</th>
    <th align="center" width="25%">微信交流群</th>
    <th align="center" width="25%">QQ 交流群</th>
    <th align="center" width="25%">添加作者微信</th>
  </tr>
  <tr>
    <td align="center" valign="middle">
      <a href="assets/community/wechat-reward.jpg">
        <img src="assets/community/wechat-reward.jpg" alt="CreatorHub 作者微信赞赏码" width="200">
      </a>
    </td>
    <td align="center" valign="middle">
      <a href="assets/community/wechat-group.png">
        <img src="assets/community/wechat-group.png" alt="CreatorHub 微信交流群二维码" width="200">
      </a>
    </td>
    <td align="center" valign="middle">
      <a href="assets/community/qq-group.jpg">
        <img src="assets/community/qq-group.jpg" alt="CreatorHub QQ 交流群二维码，群号 1048219261" width="200">
      </a>
    </td>
    <td align="center" valign="middle">
      <a href="assets/community/wechat-personal.jpg">
        <img src="assets/community/wechat-personal.jpg" alt="作者个人微信二维码，扫码添加好友" width="200">
      </a>
    </td>
  </tr>
</table>

<p align="center">
  使用微信扫码赞赏、加入微信群或添加作者；QQ 群可用 QQ 扫码，或搜索群号 <b>1048219261</b> 加入。点击图片可查看原图。
  <br>
  赞赏金额随心，感谢每一份支持！入群遇到问题，也可以添加作者微信联系。
</p>

## 友链

- [LINUX DO](https://linux.do/) — 感谢社区提供的帮助与支持。

## 使用须知

本项目用于技术学习与合规的内容管理，不提供账号、Cookie、代理或平台数据。使用时请遵守目标平台规则及所在地法律法规，并尊重内容版权和个人隐私。

## 授权说明

本项目采用**源码公开（Source-Available）**自定义授权（详见 [LICENSE](LICENSE)），而非标准 OSI 开源协议：

- **个人非商业用途**：免费授权，可直接下载、运行、学习及个人日常使用，无需提前申请。
- **商业用途（不收取授权费，须书面确认）**：企业、团队、工作室、MCN 等商业主体用于内部业务场景时，**不收取任何授权费**，但须在使用前通过上方[作者微信](#赞赏与交流)或 [GitHub Issue](https://github.com/3441293738/creatorhub/issues) 联系作者完成书面确认（说明使用主体与使用场景即可）。
- **限制条款**：未经作者另行明确书面许可，禁止将本项目（含源码、安装包或修改衍生版本）直接或变相出售、付费分发、去版权换皮套壳，或封装为商业 SaaS / 付费代管服务对外提供。
- **免责声明**：本软件按“现状（AS IS）”提供，个人免费使用及商业书面确认均不包含商业 SLA、售后保障或连带法律责任。
