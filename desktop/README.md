# Windows 桌面分发

桌面启动中心使用独立 React + Radix UI 界面，由 WebView2 原生窗口承载。不修改原 Web 工作台，也不替换 `start.cmd` / `start.sh`。

## 普通用户

安装向导默认使用简体中文，包含路径选择、快捷方式、安装/卸载按钮与 WebView2 错误提示。语言资源随仓库固定保存，不依赖构建机是否装有中文语言包。

1. 安装已发布的 `CreatorHub-Setup-版本-windows-x64.exe`，通过桌面快捷方式启动。
2. 无需自行安装 Python。安装器检测 Microsoft Edge WebView2 Runtime，缺少时自动通过随包附带的微软引导程序联网安装；失败会提示重试，不继续启动应用。首次点击“启动本地服务”会自动下载 Patchright Chromium；小红书建议安装系统 Chrome。
3. 打开桌面版只显示启动中心，不启动后台服务、不下载浏览器、不执行任务。点击“启动本地服务”后再等待就绪；是否自动打开工作台由偏好设置控制。端口 8000 被占用时自动换用空闲端口。
4. 使用“新手向导”选择平台，按登录 → 第一个任务完成操作。
5. 可最小化到托盘；关闭网页不停止任务。“停止并退出”或关闭管理窗口会确认并停止服务。

从 [最新正式版](https://github.com/3441293738/creatorhub/releases/latest) 的 Assets 下载安装包。仓库 ZIP 是源码，不是安装包。

数据位于 `%LOCALAPPDATA%\CreatorHub\user-data`，与安装目录分开：

- `config.yaml`、`data/`：配置、账号 Profile、数据库与媒体。
- `browsers/`：浏览器组件，不打进安装包，首次启动自动下载。
- `logs/`：本机诊断日志，可能含敏感信息，不要直接公开。
- `backups/`：版本变化时启动前自动备份配置和数据库，不包含媒体与 Profile；原始用户数据不会被安装器删除。

卸载保留数据并提示位置；为防误删，暂不提供安装器内一键清空账号数据。完整迁移需退出服务后备份整个 user-data，并补充自定义存储目录；源码版旧账号不会自动导入安装版。不要同时让源码版和安装版操作同一数据库或 Profile。

“导出诊断摘要”只导出版本、操作系统、进程运行状态和退出码等白名单信息，不导出日志、配置、路径或账号。

## 本地构建（Windows x64）

建议使用干净的 Python 3.11 x64 虚拟环境。打包和运行环境不写入仓库。

```powershell
python -m pip install -r requirements.txt -r desktop/requirements-build.txt -c desktop/constraints-windows.txt
npm ci
npm run build:ui
npm run build:desktop
python desktop/build_windows.py --version 0.2.0
python desktop/smoke_windows.py dist/windows/CreatorHub/CreatorHub.exe
```

生成 `dist/windows/CreatorHub/CreatorHub.exe`。这是 **onedir 便携运行目录**，分享时必须包含整个 CreatorHub 文件夹，不能只复制 EXE。

监控功能由随包的 `app/web` 与后端共同提供。工作台和启动中心均需构建；CI 会运行监控接口、秒级/自定义间隔与前端回归，并对冻结程序实际提供的 JS/CSS 与本次构建做字节比对。监控间隔始终按整秒保存，已有分钟配置不重置。只推送源码不会更新已安装客户端，需发布新版本安装包后通过“检查更新”升级。

旧版正在运行时，先退出再覆盖构建；如要保留旧版，可加 `--dist-dir dist/windows-0.2.0` 输出到仓库内的独立目录。Inno Setup 默认从 `dist/windows/CreatorHub` 取文件；使用独立构建目录时，通过 `/DAppSourceDir=绝对路径` 指定其中的 `CreatorHub` 文件夹，并保持 `/DAppVersion` 与构建版本一致。编译器的 `/O输出目录` 可将体验版安装包与旧版分开保存。

安装 [Inno Setup 6](https://jrsoftware.org/isdl.php) 后编译安装包：

```powershell
& ./desktop/prepare_webview2.ps1
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /DAppVersion=0.2.0 desktop/installer.iss
```

第一条命令下载并验证微软签名的 WebView2 引导程序；此文件为安装器的必需构建输入。其检测和安装方式遵循 [微软分发文档](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)。

输出：`dist/installer/CreatorHub-Setup-0.2.0-windows-x64.exe`。
安装器使用 `desktop/languages/ChineseSimplified.isl`，仅启用简体中文，旧安装器保存的英文语言选择不会沿用；字体与自定义提示位于 `desktop/installer.iss`。升级 Inno Setup 时需检查新增消息是否均有翻译，来源与 MIT 许可见 `desktop/languages/README.md`。
Inno Setup 许可条件以其官方页面为准。项目使用按用户安装，无需管理员权限，依据 [PrivilegesRequired 文档](https://jrsoftware.org/ishelp/topic_setup_privilegesrequired.htm)。

构建器按 [PyInstaller 运行时路径约定](https://pyinstaller.org/en/stable/runtime-information.html)区分资源位置和可写用户目录。冻结后的 `sys.executable` 是启动器而非 Python，因此后台进程通过同一个 EXE 的 `--serve` 模式启动。

## GitHub Actions

### 正式发布（开发者自用）

本节是开发者自用的发布备忘，普通用户无需操作。推送版本标签后，由 Actions 自动完成测试、构建和发布，无需其他维护者参与。

日常只维护一份根目录的 [`CHANGELOG.md`](../CHANGELOG.md)：在文件顶部增加 `## [本次版本号]`，写明用户看得懂的中文变化。完成下方发布前验收，并提交代码与更新日志后，在项目根目录运行：

```powershell
python desktop/release.py 0.2.0
```

助手会检查工作区是否干净、更新说明是否齐全、版本是否递增、标签是否重复，显示本次提交与说明，再要求输入 `v0.2.0` 确认。确认前不修改 Git；确认后只创建并推送这个标签，不自动提交代码、不推送其他分支、不覆盖旧标签。推送失败保留本地标签，可运行同一命令重试。远端标签已存在时，到 Actions 重跑失败任务；已发布版本的修复使用新版本号。

只想检查时用 `python desktop/release.py 0.2.0 --check`，不会创建或推送标签。发布助手只依赖 Python 标准库和已经能连接 `origin` 的 Git，不要求本机安装编译器或配置新的访问令牌。CI 继续负责完整测试和打包。支持 `1.2.3` / `1.2.3.4`；日常将命令参数与更新日志标题换成下一个版本即可，无需逐处修改构建器的本地预览默认值。

手动 `git tag` / `git push` 的原有流程仍保留，CI 同样要求有对应版本的更新日志。推送标签即表示发布该版本：

1. Actions 的 **Build Windows installer** 对标签对应代码运行测试、打包及冻结冒烟检查。
2. 下载并校验微软 WebView2 引导程序，编译 Setup，生成标准 `SHA256.txt`，保留 Actions artifact。
3. 仅在构建成功后，以独立的 `contents: write` 发布任务创建 Release 草稿、上传安装包与摘要，再公开发布。本版中文说明从 `CHANGELOG.md` 自动提取到 `RELEASE_NOTES.md`，同时用于 Release 正文与客户端展示，不再让用户阅读自动生成的英文提交列表。发布调用采用 [GitHub CLI](https://cli.github.com/manual/gh_release_create)。
4. 用户从 README 的最新正式版入口下载；客户端“检查更新”也使用同一 Releases 通道。GitHub 自动判断 latest，补发旧版本不强制顶替新版。

在仓库 Actions 查看运行结果。构建失败不发布；草稿上传失败可重跑失败任务。已公开的同标签版本不会被覆盖，需要修复时发布新版本号。若发布权限被组织策略限制，请允许该发布任务使用 `contents: write`。

### 仅构建测试包

在 Actions 中手动运行 **Build Windows installer**，选择分支并填入数字版本（如 `0.2.0`），该提交的 `CHANGELOG.md` 应有对应段落。手动运行只上传安装包、更新清单、可用的增量包、中文说明与 SHA256 artifact，不公开发布。适合先下载测试包进行干净机器验收，再对同一提交打正式标签。构建不上传本地配置或账号数据。

构建机需要 Inno Setup 6；工作流缺少编译器时明确失败，不静默跳过安装包生成。

## 发布前验收

- 在没有 Python 的干净 Windows 10/11 x64 测试机上安装、首次下载浏览器、打开 GUI 与托盘菜单。
- 分别验证已安装 WebView2 时跳过引导安装、缺少时自动安装，以及断网失败后恢复网络重试；本机签名下载验证和静态测试不替代干净机器安装验收。
- 测试断网、下载中断重试、8000 端口占用、重复启动以及正常退出。
- 用测试账号人工验证各平台登录与最小任务；自动冒烟测试不登录、不采集、不发布。
- 测试覆盖升级时数据保留、升级前数据库备份、卸载后数据仍存在。
- 安装包当前未配置代码签名。正式分发前配置维护者的签名证书并复核第三方许可；不要指导用户关闭系统安全防护。
- 手动升级先退出管理器，再运行新安装包。不实现后台自动下载更新或静默升级。

源码开发时可运行 `python desktop/launcher.py`。这同样使用独立的用户目录。测试可通过 `CREATORHUB_DESKTOP_HOME` 指定临时目录；该变量不用于普通用户配置。

## 桌面开发与测试

默认启动为手动模式，旧 `--no-autostart` 参数保留兼容。服务进程独立持有 `service.lock`，即使启动中心异常退出，新服务也不能同时使用同一数据目录。启动中心消失时先请求正常停止，启动或下载卡住超过 30 秒后结束本次服务进程树；源码版没有离线文档时继续使用在线教程。旧版本没有此保护，升级前仍需先退出旧版。

父进程监视使用 Windows 进程句柄，避免仅按 PID 轮询；参考 [OpenProcess](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocess) 和 [WaitForSingleObject](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject)。`tests/test_desktop_lifecycle.py` 在临时数据目录覆盖手动启动、重复服务拒绝、运行中丢失启动中心和启动卡住时的回收。

### 在线检查与一键更新

#### v0.2.0 是新更新协议的起点

- 已公开的 v0.1.0 用户先退出旧版，手动覆盖安装一次 v0.2.0；旧客户端不会因服务端发新版本而自动获得新更新能力。
- v0.2.0 发布完整 Setup 和文件清单，建立基线。下一版发布时，CI 从上一正式版下载并核验清单，生成变化文件 ZIP。比如 v0.2.0 → v0.2.1，只下载变化的 EXE、Python 打包文件、JS/CSS 或依赖；不是直接修改源码行，也不在线热替换运行中的 Python 模块。
- 首版只维护“上一正式版 → 本版”的增量路径，不串联补丁。跨越多个版本、Python/PyInstaller 运行时变化、基线不匹配、增量包不比完整包小或元数据缺失时使用完整包。仅修改代码而依赖未变时，未变化的大型依赖不会重复下载。
- 两端都核验基线与完整目标文件清单。只允许 `CreatorHub.exe` 与 `_internal/` 的受管文件；拒绝路径穿越、大小写冲突、链接/目录联接、额外 ZIP 条目和超量解压。新增/删除文件也纳入目标清单。
- 续传缓存以预期 SHA-256 命名；取消、网络中断、客户端重开后可复用。严格验证 HTTP 206 范围；服务器忽略 Range 时从零下载，不错误拼接。完整文件必须再次通过 SHA-256 后才能安装。缓存清理只触及摘要命名文件（14 天/2 GiB 策略，当前下载除外）。
- 增量安装：临时组装完整新版 → 隔离的导入/资源启动检查 → 停止状态下切换目录 → 再次启动检查。切换失败或检查失败恢复旧目录；恢复过程不运行真实服务、不迁移数据库，也不重放任务。旧目录保留在安装目录旁的 `.CreatorHub-previous-update-*`，异常暂存目录也保留用于排障，确认升级正常后可由维护者清理，不把它们当作用户数据备份。
- 目录切换不是跨目录的单次原子操作。进程/断电中断由 `runtime/update-transaction.json` 记录；下次可启动时，启动器先调用外置更新器恢复。若恰好中断在两个目录重命名之间而快捷方式目标暂时缺失，使用完整安装包修复，或用下面的外置恢复命令。不会宣称断电后的所有场景都能自动恢复。

```powershell
# 仅在上次增量更新中断、所有 CreatorHub 进程退出后使用。
# 从 user-data/runtime/updates 对应 update-* 目录找到该次独立更新器。
& '对应更新目录/CreatorHubUpdater.exe' --recover-home "$env:LOCALAPPDATA/CreatorHub/user-data"
```

维护者可用两版构建验证变化文件大小；初次 v0.2.0 不传基线：

```powershell
python desktop/build_update.py --app-dir dist/windows/CreatorHub --version 0.2.0
# 下一版（已先构建 0.2.1 并编译相应 Setup）：
python desktop/build_update.py --app-dir dist/windows/CreatorHub --version 0.2.1 --base-manifest path/to/CreatorHub-Update-0.2.0-windows-x64.json
```

发布资产：`CreatorHub-Setup-版本-windows-x64.exe`、`CreatorHub-Update-版本-windows-x64.json`、可用时的 `CreatorHub-Delta-旧版-to-新版-windows-x64.zip`，以及覆盖这些资产的 `SHA256.txt`。一律先上传草稿，再公开；旧版 v0.1.0 的 Release/标签不重写。本地先前的 0.3.0 等体验目录不是正式版本序列的一部分。

依赖约束在 `desktop/constraints-windows.txt`，用于减少每次构建时依赖自动升级造成的全量变化。它记录已测试版本，不代替持续的依赖安全维护；升级约束后重新构建、比较清单并测试。运行时身份包含 Python 和 PyInstaller 版本，构建平台/解释器不同会回到完整包。

清单与 ZIP 的信任来源仍是同一 GitHub Release 的摘要和 HTTPS，不是独立发布者签名；代码签名证书或独立签名密钥尚未配置。相关协议：[HTTP Range/Content-Range](https://www.rfc-editor.org/rfc/rfc9110.html#section-14)、[GitHub 资产 digest](https://docs.github.com/en/rest/releases/assets)。

日常入口：**启动中心新版提醒 → 一键更新 → 安装并重启 → 确认安装并重启**。也可进入 **偏好设置 → 版本与更新** 手动检查。只请求公开的 GitHub Releases/安装包，不发送账号、配置、用户目录或访问令牌；中文更新说明作为纯文本展示。

- 安装版窗口就绪 15 秒后尝试后台检查，之后按 24 小时间隔节流，检查时间跨重启保存；失败也不会频繁重试。可关闭自动检查，手动检查始终可用。源码预览和测试不会自行联网检查。
- 只提醒，不自动下载、安装或抢焦点。主页、指南和运行记录共享同一个更新组件，下载状态不随页面切换丢失；后台检查失败仅在设置中显示，不弹出错误窗口。
- “稍后提醒”对本版延后 24 小时；设置中的“忽略此版本”只影响该版本提醒，不影响下一版，也不禁止手动检查和安装。提醒偏好保存在 `runtime/updates/preferences.json`。
- 后台检查不会覆盖下载中、已下载、安装中或待处理的失败状态；已下载时保留安装入口，避免误点检查导致重新下载。退出或开始安装时关闭后台检查定时器。

- 下载阶段显示进度，支持取消、断点续传和缓存复用，不停止当前任务。安装包大小须与发布记录一致，并通过 SHA-256 校验才开放安装。
- 点击安装后明确提醒运行中任务将中断，再次校验安装包，停止服务并备份配置与数据库；账号 Profile、媒体及自定义数据目录原位保留。
- 独立更新器先复制到用户目录下的 `runtime/updates/`。主进程确认它已就绪后退出；更新器持有父进程句柄等待退出，并独占桌面/服务锁后再调用安装包，避免覆盖运行中的程序或停止其他应用。
- 安装完成只重新打开启动中心，不自动启动服务或重放任务；系统需要重启时提示用户手动重启 Windows，不自动重启电脑。
- 下载、校验、备份或安装交接失败时不替换旧程序，可重试。完整安装器已经执行后的失败会记录退出码和日志，并尝试重新打开客户端；此路径不承诺程序文件自动回滚，可用正式安装包修复。增量路径在切换或启动检查失败时恢复旧目录。账号数据不由安装器覆盖或删除。

从 **v0.2.0** 开始支持文件级增量更新与断点续传，完整安装包继续保留。源码预览保留浏览器手动下载入口，一键安装仅在已打包的 Windows 桌面客户端中开放。尚未带此更新器的旧版用户，需要先手动覆盖安装一次新版；仅更新仓库代码不会改变已安装客户端。

维护者发布约定：推送 `v0.2.0` 等三/四段数字标签，工作流自动发布正式版及 `CreatorHub-Setup-0.2.0-windows-x64.exe`，生成更新说明。draft/prerelease 不进入客户端更新通道。只有 Git tag、Actions artifact 或源码 ZIP 不等于桌面安装包，必须等待发布任务成功。

版本号按数字比较，不把 `0.10.0` 当作早于 `0.9.0`。仅接受项目 release 中名称、路径匹配的 Windows x64 安装包（上限 2 GiB）；HTTPS 跳转仅允许 GitHub 及其资产 CDN。校验依据为同一发布的 `SHA256.txt` 或 GitHub 资产 `digest`，二者均存在时必须一致；校验信息缺失时保留手动下载，不执行一键安装。SHA-256 验证与发布记录一致，不等同于发布者代码签名；当前代码签名仍需维护者配置证书。

更新日志位于 `logs/update-handoff.log`、`logs/update-*-helper.log`、`logs/update-*-setup.log`；配置与数据库备份位于 `backups/`，已下载文件位于 `runtime/updates/`。不要在安装期间清理这些目录。中断后再次打开时会提示上次更新结果；日志可能包含本机路径，请勿直接公开。

实现依据：[GitHub Releases API](https://docs.github.com/en/rest/releases/releases#get-the-latest-release)、[资产 digest](https://docs.github.com/en/rest/releases/assets)、[Inno Setup 安装参数](https://jrsoftware.org/ishelp/topic_setupcmdline.htm)、[安装退出码](https://jrsoftware.org/ishelp/topic_setupexitcodes.htm)、[PyInstaller 冻结路径](https://pyinstaller.org/en/stable/runtime-information.html)。构建器同时打包独立 `CreatorHubUpdater.exe`，CI 验证它随主程序分发。

```powershell
python -m unittest discover -s tests -p "test_desktop_update*.py"
npm run build:desktop
python tests/desktop_update_browser.py
python desktop/smoke_update_windows.py dist/windows/CreatorHub/_internal/desktop/CreatorHubUpdater.exe
python desktop/smoke_update_windows.py dist/windows/CreatorHub/_internal/desktop/CreatorHubUpdater.exe --delta
```

页面验收使用本地响应和非可执行字节样本，覆盖下载/取消/重试、安装确认、深浅主题与窄窗口。原生交接验收使用临时 C# 假应用/假安装器，验证进程退出、文件锁、带中文/空格路径的替换和重新启动，不安装真实软件或访问账号。正式发布前仍需在测试机上验证 Inno Setup 旧版→新版覆盖安装、空间不足、断电/安装中断后的修复和数据保留。

界面源码仅在 `frontend/desktop/`，构建产物仅在 `desktop/web/`。控制层在 `desktop/controller.py`，本机接口和原生窗口在 `desktop/web_shell.py`。接口使用随机令牌、Host/Origin 校验和操作白名单，不监听公网，不复用 Web 业务路由。

```powershell
npm run build:desktop
python desktop/launcher.py --no-autostart
python -m unittest discover -s tests -p test_desktop_shell.py
python desktop/launcher.py --shell-smoke-test
python tests/desktop_browser.py
```

浏览器验收脚本使用独立临时数据及 Patchright Chromium，不使用真实账号。覆盖服务启停、主题持久化、搜索与详情返回、诊断下载、取消退出、错误及离线恢复、窄窗口和减少动态效果。原生冒烟实际加载 WebView2 并验证页面导航；打包冒烟同时验证冻结资源和服务生命周期。

仅预览桌面界面：`python -m desktop.dev_server --port 8082 --home build/desktop-preview-data --skip-browser-install`。本地预览不是主 Web 端。紧急排障时可显式运行 `python desktop/launcher.py --legacy-ui` 使用旧窗口。

本地构建不代表已公开发布；正式发布以 GitHub Release 中的 Setup 及校验文件为准。托盘、系统保存对话框、干净机器首次安装仍需按发布前清单人工验收。
