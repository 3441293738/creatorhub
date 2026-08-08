# 小红书每账号独立 Chrome CDP 与自然交互设计

## 背景与目标

CreatorHub 当前通过 Playwright `launch_persistent_context()` 为每个账号启动独立的持久化浏览器环境。该实现已经做到账号 Profile 隔离，并优先使用系统 Chrome，但浏览器仍由 Playwright 直接启动。小红书登录过程中已经出现与用户日常浏览器不同的设备安全验证页面，因此本次引入 CDP 后端，让 CreatorHub 自行启动系统 Chrome，再通过 Chrome DevTools Protocol 连接该浏览器。

复查现有小红书路径后，还发现仅替换连接方式不足以达到目标：发布当前优先走 API 直发，搜索框使用瞬时 `fill()`，多个列表采用固定等待和单次 2500～4000 像素大步滚动，部分路径直接修改 DOM `scrollTop`，浏览器读取还可能屏蔽图片、媒体和字体。这些行为即使运行在系统 Chrome 中，也与正常页面操作存在明显差异。因此本设计同时覆盖“真实环境一致性”和“自然页面交互”，而不是把 CDP 当作单独的风控开关。

本次目标是：

1. 小红书每个账号使用独立的系统 Chrome 进程、Profile、CDP 端点和代理环境。
2. 保留系统 Chrome 原生语言、时区、窗口、资源加载、缓存和浏览器状态，不注入伪造指纹。
3. 登录、手动打开浏览器和需要浏览器的小红书后台任务复用同一账号会话。
4. 浏览器写操作默认经过可见页面和正常控件；页面读取使用条件等待、有限步进滚动和合理节奏。
5. 保留当前 Playwright 持久化 Context 作为兼容回退，没有系统 Chrome 的用户仍可使用项目。
6. 完整覆盖现有 HTTP、认证 HTTP、SOCKS5 和认证 SOCKS5 代理配置，并保证代理失败时不会静默直连。
7. 不改变抖音、快手和视频号的浏览器路径，不扩大首期回归范围。

设计优先级依次为：环境与账号长期一致、页面操作语义正确、节奏与并发合理、失败结果可确认。随机移动鼠标或堆叠固定休眠不能代替这些基础。CDP 和自然交互只减少异常环境与机械操作信号，不承诺消除平台验证；验证码、设备安全验证和平台限制仍按现有风险处理流程处理。

## 已确认的设计决策

- 首期仅对小红书启用 CDP。
- 采用“程序启动每账号独立 Chrome CDP”，不连接用户日常 Chrome 默认 Profile。
- CDP 默认采用有头系统 Chrome；正在执行页面交互时保持正常窗口和活动标签页，只有空闲会话才可最小化，不切换为 Headless。
- `BrowserManager` 继续作为调用方的统一入口，平台代码不直接管理 Chrome 进程或 CDP 端口。
- 同一账号的浏览器操作串行；全机同一时刻只执行一个需要可见页面的主动小红书交互。纯下载和不操作页面的任务仍受现有锁控制，可按现有规则并行。
- 默认配置为 `auto`：优先 CDP，失败后使用现有 Playwright 模式。
- 小红书发布和评论默认使用浏览器页面；API 写入仅保留为显式兼容模式，页面已开始提交后绝不自动切换 API 重试。
- 自然交互采用条件等待加有界节奏变化，不使用无意义的随机游走、夸张鼠标轨迹或重复固定延迟。
- 代理凭据不得进入 Chrome 命令行、Profile、日志或 API 响应。
- MediaCrawler 仅作为 CDP 架构参考。本项目独立实现，不复制其受 `NON-COMMERCIAL LEARNING LICENSE 1.1` 限制的源代码。

## 不在本次范围内

- 不把 CDP 推广到抖音、快手或视频号。
- 不连接或接管用户日常 Chrome 的默认 Profile、标签页、Cookie 或扩展。
- 不添加反检测脚本、指纹伪造、`AutomationControlled` 修改或关闭浏览器沙箱的参数。
- 不把现有小红书非写入 HTTP/Node 路径全部改写成浏览器内请求；本次只把高风险写操作的默认路径改为页面交互。
- 不通过覆盖 `navigator.webdriver`、注入 getter 或篡改浏览器 API 来伪装环境。
- 不为了“看起来随机”而牺牲操作正确性、可测试性或任务结果确认。
- 不调整平台接口、数据库表结构或账号数据模型。
- 不保证解决所有验证码或平台设备验证。

## 总体架构

```text
BrowserManager（现有统一入口）
├─ 小红书 + auto/cdp
│  └─ XhsCdpBackend
│     ├─ ChromeLocator
│     ├─ CdpProcessLauncher
│     ├─ CdpSessionRegistry
│     ├─ ProxyPlan / ProxyAuthController
│     ├─ Socks5AuthRelay（仅认证 SOCKS5）
│     ├─ XhsInteractionPolicy
│     └─ XhsVisibleActionGate
└─ 其他平台或 auto 回退
   └─ 现有 PersistentContextBackend
```

### BrowserManager

`BrowserManager` 保持现有公开方法和账号锁语义。它根据账号平台、配置模式和运行环境选择后端，并统一执行容量限制、LRU 驱逐、关闭、环境快照和错误归一化。现有平台调用点无需知道返回的 Context 来自 CDP 还是 `launch_persistent_context()`。

### ChromeLocator

只查找本机稳定版 Google Chrome 的标准安装位置，覆盖 Windows、macOS 和 Linux。未找到 Chrome 不视为项目启动失败：`auto` 模式进入兼容回退，`cdp` 模式返回明确错误。首期不把 Edge 伪装成 Chrome，也不复用 Playwright 下载的 Chromium 作为“系统 Chrome CDP”。

### CdpProcessLauncher

负责以账号 Profile 启动和停止 Chrome、等待 CDP 就绪、读取实际调试端点并记录进程所有权。启动前先让操作系统选择一个可用端口，把得到的**非零端口号**显式传给 Chrome，并在端口竞争时重新选择后重试；CDP 仅监听 `127.0.0.1`。不得把 `--remote-debugging-port=0` 直接传给 Chrome，因为 Chromium 会为该取值启用 `AutomationControlled`。启动成功后在账号 Profile 旁保存不含凭据和 CDP 地址的所有权标记，只记录 PID、浏览器可执行文件、Profile 和启动时间；它用于崩溃恢复时区分 CreatorHub 专用 Chrome 与其他进程。

启动参数保持最小集合：

- 账号现有 `identity.profile_dir`；
- 本地 CDP 地址和随机选择的非零端口；
- 首次运行提示与默认浏览器提示控制；
- 账号代理服务器参数；
- 仅在配置代理时使用现有 WebRTC 非代理 UDP 限制。

不得加入 `--remote-debugging-port=0`、`--remote-debugging-address=0.0.0.0`、`--no-sandbox`、`--disable-blink-features=AutomationControlled` 或同类参数。连接后只读取 `navigator.webdriver` 做环境自检；不得通过脚本覆盖该属性。若系统 Chrome 在没有上述自动化参数的情况下仍暴露异常值，则报告环境异常而不是注入伪装。

### CdpSessionRegistry

以账号身份键为索引保存：Chrome 进程、进程所有权、CDP Browser、默认 Context、最近使用时间、Profile 路径、代理配置签名和临时代理资源。注册表与现有 `max_live_contexts` 共享容量预算，不额外放大常驻浏览器数量。

### XhsInteractionPolicy

集中提供页面可见性、条件等待、输入、点击、滚动和提交确认语义。调用方描述“点击可见按钮”“输入短文本”“粘贴长正文”“逐步滚动至目标”等意图，不在各业务文件散落固定休眠和随机数。策略依赖可注入的时钟与随机源，生产环境获得有界节奏变化，测试环境保持确定性。

### XhsVisibleActionGate

区分主动页面交互和被动任务。登录、搜索输入、滚动、评论、私信、关注和发布等主动操作必须获得全机小红书可见操作许可，并把对应 Chrome 窗口恢复为正常状态、将目标页置前；任务完成后释放许可。这样避免同一台机器同时对多个账号产生不可能由一个正常用户完成的点击和输入序列。

## 配置

在 `engine` 下新增：

```yaml
engine:
  xhs_browser_mode: auto          # auto / cdp / playwright
  xhs_cdp_idle_seconds: 900       # 0 表示仅按 LRU 和程序关闭回收
  xhs_publish_mode: browser       # browser / api；api 仅为显式兼容模式
  xhs_comment_write_mode: browser # browser / api / manual
```

模式语义：

- `auto`：小红书优先系统 Chrome CDP；环境或启动阶段失败时回退现有 Playwright 持久化 Context。
- `cdp`：严格使用 CDP；缺少 Chrome、CDP 启动失败或代理初始化失败时返回错误，不改变为另一种浏览器模式。
- `playwright`：关闭 CDP，继续使用当前 Playwright 持久化 Context；自然交互和写入模式仍按各自配置执行。
- `xhs_publish_mode: browser`：发布从创作平台页面完成并等待可确认结果；`api` 只在用户显式选择时使用当前直发实现。
- `xhs_comment_write_mode: browser`：通过笔记页面发表评论或回复；`api` 保留现有接口写入，`manual` 继续只生成草稿。

旧 `config.yaml` 缺少新字段时按上述默认值加载，无需用户手工迁移。已有配置明确写成 `api` 时尊重用户选择，但启动诊断和 README 将其标记为兼容模式，不描述为自然页面操作。`max_live_contexts` 同时计算 CDP Chrome 和旧 Context；账号活动并发仍由 `active_accounts` 等现有设置约束。

## Chrome 启动与连接

1. `BrowserManager` 收到小红书账号的浏览器请求后，先获取账号级异步锁。
2. 如果注册表存在健康会话且 Profile、代理签名和模式均匹配，直接复用。
3. 如果注册表无会话，检查账号 Profile 中的 CDP 活动端点：只有所有权标记、PID、浏览器可执行文件和 Profile 均匹配，且端点仍在本机回环地址上可用时，才允许恢复并接管该专用 Chrome。无标记或验证失败的进程不连接、不终止。
4. 没有可恢复端点时，启动新的系统 Chrome。
5. 等待 CDP 端点就绪后，通过 Playwright 1.61 的 `connect_over_cdp(..., is_local=True, no_defaults=True)` 连接。
6. 使用 Chrome 的默认持久化 Context；若无法取得默认 Context，则启动视为失败，不创建不持久化登录态的临时 Context 来掩盖问题。
7. 会话通过代理和基本页面健康检查后才写入注册表。

同一 Profile 在任一时刻只允许一个 Chrome 进程。检测到 Profile 被占用时先尝试重新连接；端点无效且仍被其他进程占用时返回清晰错误，不启动第二个进程。

## 真实环境一致性

- CDP 使用系统 Chrome 自己的 User-Agent、Client Hints、语言、时区、缩放、DPR、字体、媒体能力和硬件画像；不从账号库重新覆盖这些值。
- `no_defaults=True` 避免 Playwright修改默认 Context 的下载、焦点和媒体模拟设置。
- 账号 Profile 长期复用 Cookie、Local Storage、IndexedDB、缓存、Service Worker、权限选择和窗口尺寸，不在每次任务中随机改变环境。
- 小红书 CDP 页面加载完整图片、样式、字体、脚本和正常媒体资源；不得沿用读取路径中对 image、media、font 的统一拦截。只有用户明确触发的实际文件下载仍按下载器处理。
- 代理、系统时区和账号区域保持稳定；同一 Chrome 会话中不热切换网络出口。
- 正在进行页面操作的窗口保持正常状态，目标标签页先 `bring_to_front()`；空闲后才允许最小化。不得在页面处于最小化或非活动标签状态时注入点击、键盘或滚轮事件。
- 环境健康检查记录 Chrome 版本、Context 类型、页面可见性以及原生 `navigator.webdriver` 结果。检查只读不改；异常时报告和回退，不注入补丁。

## 自然交互策略

自然交互不是全局随机休眠，而是“先等待真实页面状态，再在动作之间加入小范围、不可同步的节奏变化”。所有范围由 `XhsInteractionPolicy` 集中维护，并允许测试注入固定时钟和随机源。

### 点击与导航

- 优先使用语义稳定的 Locator，要求元素可见、可用且完成必要的滚入视区。
- 点击前允许短暂停留或 `hover()`，随后使用正常 `click()`；默认不使用 `force=True`、坐标猜测或 JavaScript `element.click()`。
- 第一次进入主站或创作平台时从主页、搜索入口或创作中心等正常入口开始；任务本身来自分享链接、作品链接时允许正常深链导航。
- 页面改版导致正常控件不可用时返回可诊断错误，不用多层 JS 点击链掩盖失败。

### 文本输入

- 搜索词、短标题和短评论在聚焦输入框后按字符输入，字符间隔和停顿在窄范围内变化。
- 长正文通常由正常用户粘贴，不做数分钟逐字模拟；聚焦编辑器后使用一次受控文本插入，再触发正常的 `input`/`change` 路径。
- 输入后读取控件值或可见文本确认内容正确，再进入下一步。
- 不使用瞬时 `fill()` 作为小红书主动交互的默认路径；它只能作为明确记录的兼容兜底，且不能在提交阶段静默启用。

### 滚动与等待

- 以 350～900 像素的有限滚轮步进滚动，结合实际新增内容、目标元素可见或加载指示状态决定是否继续。
- 步进之间使用小范围节奏变化，不使用每次完全相同的 1500/1800/2500 毫秒等待。
- 不再通过 `scrollTop = scrollHeight`、`window.scrollBy(0, 3000+)` 或单次 2500～4000 像素滚轮跳到底部。
- 网络响应、上传完成、按钮可用和成功提示优先使用条件等待；超时只作为上限，不把固定休眠当作成功判据。

### 可见操作并发

- 登录、搜索、页面滚动、评论、私信、关注和发布属于主动可见操作，必须经过全机容量为 1 的 `XhsVisibleActionGate`。
- 文件下载、媒体处理和不控制页面的 HTTP 读取不占用可见操作许可，但仍受现有账号、网络出口和风险控制锁约束。
- 等待验证码期间保留当前账号许可，避免另一个账号窗口在用户处理验证时抢占焦点。

### 任务节奏与状态连续性

- 继续以现有风险控制事件、账号最小间隔、小时/每日额度、活跃时段和 `scan_jitter` 为唯一调度依据，不在交互层另造一套可绕开的额度。
- 启动恢复时不让所有账号立即同时补跑；沿用持久化历史和抖动逐步恢复到期任务。
- 同一页面会话尽量连续完成一个逻辑任务，不在每个步骤之间反复关闭、重开或清空缓存。
- 条件等待只等待完成当前动作所需的页面状态；失败后不进行高速刷新、连续重新导航或无限选择器轮询。
- 手动“立即执行”仍需经过账号锁、网络出口锁、可见操作许可和统一风险闸门；它只能绕过已有设计明确允许绕过的排期等待。

## 页面与任务数据流

### 登录和手动打开

- 登录使用默认 Context 中的可见页面。
- 已有登录页时复用并置前，不重复创建扫码页面。
- 用户处理验证码或设备安全验证期间，会话保持打开，后台任务不抢占该页面。
- 点击“打开浏览器”时优先恢复账号已有窗口和标签页；跨平台窗口恢复能力不足时至少调用页面置前并给出可见提示。

### 后台浏览器任务

- 后台任务借用同一账号 Context，但创建独立临时任务页。
- 页面在任何导航前完成代理认证监听器安装。
- 任务结束只关闭临时页，不关闭 Context、Chrome 或登录状态。
- 同一账号浏览器任务串行，避免登录、监控、发布和响应监听互相抢占页面。
- 不同账号的非页面任务可在现有 `active_accounts`、网络出口锁和平台风险预算内并行；主动页面任务还必须依次取得 `XhsVisibleActionGate`。

### 发布与评论

- `xhs_publish_mode: browser` 时，从创作平台可见入口进入发布页，点击图文/视频类型，使用页面文件控件上传，等待上传和处理状态完成，再输入标题与正文并点击一次发布。
- 发布成功以平台响应、成功页面或明确成功提示确认。已经点击发布但未得到确定结果时标记为“不确定”，不得调用 API 或再次点击重试。
- `xhs_comment_write_mode: browser` 时，打开目标笔记，按有限步进滚动到评论区，定位可见输入框，输入内容并点击发送；回复任务找不到目标评论时失败，不降级成顶层评论。
- 评论成功以发表接口响应和页面可见结果交叉确认。提交后连接中断同样保留不确定状态，不切换 API 重发。
- 发布和评论继续经过已有审核、活跃时段、最小间隔、小时/每日额度、账号锁、网络出口锁和统一风险控制。
- 显式 `api` 模式保持现有实现，但 UI、日志和任务结果记录实际方法，便于用户知道该操作没有经过页面交互。

### 直连任务

现有小红书非写入 HTTP、`curl_cffi`、Node 签名和媒体下载路径维持现状，继续使用账号代理和当前风险闸门。CDP 不会自动改变这些请求的网络路径。发布和评论按新配置选择页面或显式 API 模式；其他需要浏览器 DOM、响应监听或页面登录态的任务走 CDP Context。

## 会话生命周期

- 每次借用和归还更新最近使用时间。
- 超过 `xhs_cdp_idle_seconds` 的空闲会话可被回收；值为 `0` 时只在 LRU、显式关闭或程序退出时回收。
- 达到 `max_live_contexts` 时沿用现有 LRU 规则驱逐最久未使用且未被任务占用的会话。
- 关闭顺序为：停止接受新任务、关闭临时页和 CDP 会话、优雅退出本项目拥有的 Chrome、等待 Profile 刷盘、关闭本地代理资源。
- 当前进程只主动终止本次启动或经所有权标记完整验证后接管的 Chrome；不得按浏览器名称批量结束用户进程。
- 程序异常退出后，下次启动可通过所有权标记和账号 Profile 中仍有效的本地 CDP 端点恢复遗留的专用 Chrome。进程退出或回收完成后删除所有权标记；过期标记仅可清理文件，不能作为结束未知 PID 的依据。

## 完整代理设计

### 代理计划

现有账号代理字符串继续作为唯一配置来源。启动前将其规范化为内部 `ProxyPlan`，包含协议、主机、端口、是否认证及脱敏标识。无效代理配置直接失败，不把无效值当作“未配置代理”。

代理配置的规范化结果参与会话签名。账号代理发生变化时，旧 Chrome 与代理资源必须回收并重建，禁止在已连接的 CDP Context 上假装热切换代理。

### 无认证 HTTP 与 SOCKS5

Chrome 启动时通过 `--proxy-server` 使用代理服务器。代理存在时保留 WebRTC 非代理 UDP 限制，避免 STUN 绕过配置出口。

### 认证 HTTP

Chrome 命令行仅包含不带用户名和密码的代理地址。Chrome 以 `about:blank` 启动；登录页、手动页面、后台任务页及后续弹出页都必须在首次受控导航前创建 CDP Session，并启用 `Fetch` 的认证事件。事件处理器先完成注册，再启用请求暂停：

- 普通 `requestPaused` 立即继续，不修改请求；
- 仅当挑战来源为 `Proxy`，且挑战 origin 与当前账号代理匹配时，调用 `continueWithAuth` 提供凭据；
- 网站的 `Server` 认证挑战采用默认处理，绝不提供代理凭据；
- 同一请求连续认证失败达到限制后取消认证并报告代理错误，避免无限重试。

认证建立后仍对后续新页面安装相同处理器，不能依赖 Chrome 的临时凭据缓存作为正确性保证。

### 认证 SOCKS5

Chrome 本身连接账号专属的本地无认证 SOCKS5 转发器；转发器再使用用户名和密码连接上游 SOCKS5：

- 本地监听使用 `127.0.0.1` 和操作系统随机端口；
- 只支持 Chrome 所需的 TCP CONNECT，不开放公网监听或 UDP ASSOCIATE；
- 域名目标以 SOCKS 域名类型转发给上游，避免本地 DNS 解析泄漏；
- 凭据只保存在内存对象中，不写入生成文件；
- 转发器生命周期绑定账号 CDP 会话，回收会话时一并关闭。

### 失败关闭与验证

- 配置代理的账号在任何浏览器模式下都必须继续使用同一代理。
- CDP 代理启动、认证或出口检查失败时，`auto` 可以回退旧 Playwright 模式，但回退仍必须携带同一代理。
- 旧模式也无法使用代理时，终止账号任务；不允许静默直连。
- 会话投入使用前执行代理连通性检查；开启现有 `verify_proxy_region` 时继续执行出口区域验证。
- 日志和 API 只输出代理协议、脱敏主机或“已配置”状态，不输出密码和完整认证 URL。

## 错误处理与重试

| 场景 | `auto` | `cdp` | 任务重试 |
| --- | --- | --- | --- |
| 未安装系统 Chrome | 回退旧模式 | 返回环境错误 | 不适用 |
| Chrome 启动/CDP 握手超时 | 清理本次资源后回退一次 | 返回启动错误 | 不重复启动循环 |
| Profile 被其他进程占用且不可连接 | 返回冲突错误 | 返回冲突错误 | 等用户处理 |
| 代理初始化或认证失败 | 仅可带同一代理回退 | 返回代理错误 | 不允许直连 |
| Chrome 在读取任务中崩溃 | 重建后重试一次 | 重建后重试一次 | 最多一次 |
| Chrome 在写任务中断开 | 保留不确定结果 | 保留不确定结果 | 不自动重试 |
| 页面控件缺失或不可用 | 返回页面改版诊断 | 返回页面改版诊断 | 不用 JS 强点或 API 兜底 |
| 验证码/设备安全验证 | 保持 Chrome 和页面 | 保持 Chrome 和页面 | 等人工处理 |
| 用户主动关闭专用 Chrome | 下次任务重建 | 下次任务重建 | 按任务类型处理 |

发布、评论等写操作可能在响应丢失前已经到达平台，因此连接异常不得触发盲目重试。读取任务的单次恢复重试仍须经过现有账号锁、网络出口锁和风险预算。

任务取消必须释放账号锁、临时页、未注册的 Chrome 进程和代理资源；已注册且仍健康的共享会话可继续保留。清理异常只记录脱敏日志，不覆盖原始任务异常。

## 安全与隐私约束

1. CDP 始终绑定回环地址并使用随机选择的非零端口，不暴露到局域网或公网，也不触发 `remote-debugging-port=0` 的自动化运行特征。
2. 不使用用户默认 Chrome Profile；每个账号继续使用 CreatorHub 管理的独立目录。
3. 不在命令行、日志、环境快照、异常字符串、测试快照或前端状态中泄露代理凭据。
4. 不关闭 Chrome 沙箱，不添加伪装参数，不覆盖 `navigator.webdriver` 或其他浏览器 API。
5. 进程清理严格依据本次启动记录或完整验证后的所有权标记，不扫描或结束用户日常浏览器；所有权标记不保存 CDP 地址或任何凭据。
6. 本地 SOCKS 转发器只接受本机连接，并随账号会话销毁。
7. 浏览器环境状态只记录模式、浏览器名称和版本、是否配置代理等诊断信息，不记录 CDP WebSocket 地址。

## UI、诊断与文档

账号面板显示当前实际模式：

- `系统 Chrome · CDP`
- `Playwright Chromium · 回退`

写任务同时显示实际执行方式：

- `浏览器页面`
- `API 兼容模式`
- `人工草稿`

发生回退时显示不含敏感信息的简短原因，例如“未检测到系统 Chrome”“CDP 启动超时”。验证码和设备验证使用单独状态，不误报成浏览器故障。

环境快照增加实际后端和 Chrome 版本，继续保留现有 Profile、Headless 和代理布尔状态。README 说明：

- 小红书默认优先使用每账号独立系统 Chrome CDP；
- 没有 Chrome 时自动回退；
- CDP 不使用用户日常默认 Profile；
- 后台任务会复用专用 Chrome，主动页面操作期间保持正常窗口和活动标签，空闲后才可能最小化；
- 小红书页面加载完整资源，写操作默认经过页面，API 写入是显式兼容模式；
- 自然交互使用条件等待、有限滚动和合理输入节奏，不使用指纹篡改；
- 配置代理时失败不会改为直连；
- 可通过 `xhs_browser_mode: playwright` 只关闭 CDP；如需完整兼容旧写入路径，还须显式设置 `xhs_publish_mode: api` 和 `xhs_comment_write_mode: api`。

## 兼容性与发布策略

- 数据库无变更，无需迁移。
- 现有账号 Profile 路径不变，首次 CDP 启动直接复用登录状态。
- 旧配置缺少新字段时默认为 `auto`、900 秒空闲回收、浏览器发布和浏览器评论；已有显式 `api` 选择保持不变。
- 现有 `BrowserManager` 公开接口保持兼容，平台实现不进行无关重构。
- 抖音、快手和视频号继续使用现有浏览器后端。
- 如 CDP 在特定 Chrome 版本出现兼容问题，用户可以用配置切回 `playwright`，无需降级数据库或 Profile。

## 预计修改边界

- `app/config.py`、`config.example.yaml`：新增浏览器与发布模式配置，调整小红书评论默认模式。
- `app/browser/manager.py` 及新的 CDP 子模块：后端选择、Chrome 生命周期、Session 注册、代理认证、认证 SOCKS5 转发和环境快照。
- `app/browser/login.py`：小红书登录页复用、窗口可见性和代理处理。
- `app/browser/xhs_fetcher.py`：完整资源加载、条件等待、有限滚动和搜索输入。
- `app/browser/account_hub.py` 的小红书分支：移除正常路径中的大步滚动和 DOM 点击/滚动改写，接入交互策略。
- `app/platforms/xhs/publish.py`：浏览器发布改为默认、复用 CDP Context、提交结果确认和 API 兼容模式。
- `app/engine/monitor.py`：小红书浏览器评论路径、实际执行方法记录和现有风险闸门复用。
- 前端账号状态、README、测试和本地测试夹具。

现有 `app/platforms/xhs/client.py`、`creator_api.py` 和签名实现只作为显式 API 兼容路径保留，不在本次重写算法或扩展接口能力。其他平台文件仅允许为保持 `BrowserManager` 接口兼容而做必要调整。

## 自动化测试

### 单元测试

1. Windows、macOS、Linux Chrome 路径检测及无 Chrome 分支。
2. 启动参数包含独立 Profile、本地随机非零 CDP 端口、代理和必要 WebRTC 限制；不包含 `remote-debugging-port=0`、敏感或禁止参数。
3. 同账号并发创建只启动一次，不同账号使用不同 Profile、端点和注册记录。
4. 会话健康检查、代理签名变化、空闲回收、LRU 和 owned 进程清理。
5. `auto`、`cdp`、`playwright` 三种浏览器模式，`browser`、`api`、`manual` 写入模式和旧配置默认值。
6. CDP 断开、启动超时、Profile 占用、遗留端点恢复和任务取消。
7. HTTP、认证 HTTP、SOCKS5、认证 SOCKS5 的代理计划。
8. CDP 只向匹配的 `Proxy` 挑战提供凭据，网站 `Server` 挑战不接收代理密码。
9. 认证 SOCKS5 转发器的握手、域名转发、失败关闭和并发清理。
10. 代理失败后没有直连请求，回退路径继续携带相同代理。
11. 日志、异常、环境快照和 API 响应的凭据脱敏。
12. 读取任务只恢复重试一次，写任务不自动重复执行，页面控件失败不会静默切换 JS 强点或 API。
13. `XhsVisibleActionGate` 保证不同账号的主动页面操作不并行，取消和异常后许可可恢复。
14. `XhsInteractionPolicy` 使用注入时钟和随机源验证有界输入、点击和滚动序列，不产生固定节奏或超大滚动。
15. CDP 小红书页面不启用 image、media、font 资源拦截；其他平台的资源策略保持不变。
16. 浏览器发布是默认路径，调用顺序包含上传完成和提交确认；点击提交后任何异常都不会调用 API。
17. 浏览器评论是默认路径，回复目标缺失时不会变成顶层评论或 API 评论。

### 本地集成测试

使用可注入的 Chromium/Chrome 可执行文件和本地测试服务，不访问真实平台：

1. 外部启动浏览器并通过 CDP 连接默认 Context。
2. 验证传入非零调试端口后 `navigator.webdriver` 保持 Chrome 原生结果，且实现未注入覆盖脚本。
3. Cookie、缓存和本地状态写入、关闭、重新启动和持久化恢复。
4. 两账号同时运行时 Cookie、Profile、进程、端点和代理隔离，但主动页面动作由全机许可串行。
5. 本地 HTTP 407 代理通过 CDP 认证后访问测试页面。
6. 本地认证 SOCKS5 上游通过账号转发器访问测试页面。
7. 代理故障时目标测试服务器未收到直连请求。
8. 浏览器异常退出后读取任务恢复，写任务保持不确定结果。
9. 空闲回收后再次按需启动并恢复 Profile。
10. 本地交互测试页验证窗口/标签可见、完整资源加载、条件等待、有限滚动、短文本逐字输入和长文本受控插入。

CI 中不依赖真实小红书账号或验证码。缺少系统 Chrome 的 CI 通过依赖注入或 Playwright Chromium 验证协议生命周期，同时保留 Chrome 路径检测的纯单元测试。

### 回归与人工验收

- 全量 `pytest`、`selftest.py` 和 Python 编译检查通过。
- 前端脚本语法检查和依赖审计通过。
- 抖音、快手、视频号现有登录、监控和发布测试保持通过。
- 新增小红书账号打开系统 Chrome 专用 Profile，而不是用户默认 Profile。
- Chrome 命令行使用随机非零调试端口，没有 `enable-automation`、`remote-debugging-port=0`、关闭沙箱或指纹修改参数。
- 页面完整加载图片、样式、字体和正常媒体，语言、时区、窗口尺寸及浏览器状态跨任务保持稳定。
- 扫码登录后重启项目仍保留登录状态。
- 后台监控复用该账号 Chrome，不为每轮扫描创建新 Profile。
- 点击“打开浏览器”能够恢复对应账号窗口。
- 多账号后台运行时，不会同时对两个小红书窗口执行点击、输入或滚动。
- 搜索、滚动、发布和评论不再使用固定节奏、超大跳跃滚动或 DOM 直接改写作为正常路径。
- 默认发布和评论经过浏览器页面；提交结果不确定时不会重复点击或切换 API 重发。
- 多账号代理出口彼此独立，代理失效时没有直连。
- 没有 Chrome 的电脑仍能使用 Playwright Chromium。
- 验证码出现时页面保持可操作。
- 测试结束后不存在临时 Profile、CDP 端点、代理凭据、测试浏览器或本地转发器残留。

## 分阶段交付与启用

1. **CDP 基础层**：Chrome 查找、显式非零端口、进程所有权、Context 连接、会话注册和回退；此阶段不改变默认写入路径。
2. **代理层**：四类代理、认证处理、失败关闭、出口验证和凭据脱敏；通过无直连测试后才允许代理账号启用 CDP。
3. **自然交互层**：可见操作许可、完整资源、窗口状态、条件等待、输入和有限滚动；先迁移登录、搜索和读取路径。
4. **页面写入层**：浏览器发布、浏览器评论、提交结果确认和不确定状态；完成后再把新安装配置的默认写入模式切换为 `browser`。
5. **综合验证**：跨平台回归、不同 Chrome/代理环境的本地测试、README/UI 和最终代码审阅。

每一阶段保持独立测试通过后再进入下一阶段。代码可以在同一功能分支连续完成，但在 CDP、代理和页面写入全部通过前，不把未完成路径作为默认行为交付。

## 完成标准

1. 上述自动化测试和现有全量测试全部通过。
2. 小红书 `auto` 模式在有 Chrome、无 Chrome、无代理和四类代理配置下行为符合设计。
3. 同账号不会出现双 Chrome 或 Profile 锁竞争；多账号会话保持隔离。
4. 任何代理失败路径均有测试证明不会静默直连。
5. 默认小红书写操作经过页面，写任务在连接不确定时不会自动重复提交或切换 API。
6. 主动页面操作在全机范围串行，完整资源加载和自然交互策略经过确定性测试。
7. Chrome 使用非零 CDP 端口且未注入自动化属性覆盖；原生环境自检通过。
8. 代码差异通过交互语义、并发、资源所有权、敏感信息和跨平台兼容性复审。

## 参考资料

- [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)：参考其连接既有 Chrome Context 的 CDP 思路，不复制实现。
- [MediaCrawler 许可证](https://github.com/NanmiCoder/MediaCrawler/blob/main/LICENSE)：非商业学习许可证约束。
- [Chrome 远程调试变更](https://developer.chrome.com/blog/remote-debugging-port)：Chrome 136 起要求远程调试配合非默认用户数据目录。
- [Playwright `connect_over_cdp`](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp)：CDP 连接、`is_local`、`no_defaults` 及功能完整度说明。
- [Chrome DevTools Protocol Fetch domain](https://chromedevtools.github.io/devtools-protocol/tot/Fetch/)：代理认证挑战和 `continueWithAuth`。
- [Chromium 代理启动参数](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/chrome/app/resources/manpage.1.in)：HTTP、SOCKS4、SOCKS5 代理参数格式。
- [Chromium AutomationControlled 触发逻辑](https://chromium.googlesource.com/chromium/src/+/HEAD/content/child/runtime_features.cc)：`remote-debugging-port=0` 会启用自动化运行特征，显式非零端口不会走该分支。
