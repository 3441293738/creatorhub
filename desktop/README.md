# Windows 桌面分发

桌面启动中心使用独立 React + Radix UI 界面，由 WebView2 原生窗口承载。不修改原 Web 工作台，也不替换 `start.cmd` / `start.sh`。

## 普通用户

1. 安装维护者发布的 `CreatorHub-Setup-版本-windows-x64.exe`，通过桌面快捷方式启动。
2. 需要 Microsoft Edge WebView2 Runtime；缺少时从微软官网下载并安装。无需自行安装 Python。首次启动仍需联网下载 Patchright Chromium；小红书建议安装系统 Chrome。
3. 打开桌面版只显示启动中心，不启动后台服务、不下载浏览器、不执行任务。点击“启动本地服务”后再等待就绪；是否自动打开工作台由偏好设置控制。端口 8000 被占用时自动换用空闲端口。
4. 使用“新手向导”选择平台，按登录 → 第一个任务完成操作。
5. 可最小化到托盘；关闭网页不停止任务。“停止并退出”或关闭管理窗口会确认并停止服务。

目前没有发布到 GitHub Releases；测试包需先构建，不能把仓库 ZIP 当作安装包。

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
python -m pip install -r requirements.txt -r desktop/requirements-build.txt
npm ci
npm run build:desktop
python desktop/build_windows.py --version 0.2.2
python desktop/smoke_windows.py dist/windows/CreatorHub/CreatorHub.exe
```

生成 `dist/windows/CreatorHub/CreatorHub.exe`。这是 **onedir 便携运行目录**，分享时必须包含整个 CreatorHub 文件夹，不能只复制 EXE。

旧版正在运行时，先退出再覆盖构建；如要保留旧版，可加 `--dist-dir dist/windows-0.2.2` 输出到仓库内的独立目录。该选项只改变便携目录；Inno Setup 默认仍从 `dist/windows/CreatorHub` 取文件，编译安装包前应使用默认目录构建。

安装 [Inno Setup 6](https://jrsoftware.org/isdl.php) 后编译安装包：

```powershell
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /DAppVersion=0.2.2 desktop/installer.iss
```

输出：`dist/installer/CreatorHub-Setup-0.2.2-windows-x64.exe`。
Inno Setup 许可条件以其官方页面为准。项目使用按用户安装，无需管理员权限，依据 [PrivilegesRequired 文档](https://jrsoftware.org/ishelp/topic_setup_privilegesrequired.htm)。

构建器按 [PyInstaller 运行时路径约定](https://pyinstaller.org/en/stable/runtime-information.html)区分资源位置和可写用户目录。冻结后的 `sys.executable` 是启动器而非 Python，因此后台进程通过同一个 EXE 的 `--serve` 模式启动。

## GitHub Actions

在 Actions 中手动运行 **Build Windows installer**，填入数字版本（如 `0.2.2`）。流程运行测试、打包、冻结环境冒烟检查，再用 Inno Setup 编译并上传安装包和 SHA256 摘要。它不会自动发布 Release，也不会上传本地配置或账号数据。

构建机需要 Inno Setup 6；工作流缺少编译器时明确失败，不静默跳过安装包生成。

## 发布前验收

- 在没有 Python 的干净 Windows 10/11 x64 测试机上安装、首次下载浏览器、打开 GUI 与托盘菜单。
- 测试断网、下载中断重试、8000 端口占用、重复启动以及正常退出。
- 用测试账号人工验证各平台登录与最小任务；自动冒烟测试不登录、不采集、不发布。
- 测试覆盖升级时数据保留、升级前数据库备份、卸载后数据仍存在。
- 安装包当前未配置代码签名。正式分发前配置维护者的签名证书并复核第三方许可；不要指导用户关闭系统安全防护。
- 手动升级先退出管理器，再运行新安装包。不实现后台自动下载更新或静默升级。

源码开发时可运行 `python desktop/launcher.py`。这同样使用独立的用户目录。测试可通过 `CREATORHUB_DESKTOP_HOME` 指定临时目录；该变量不用于普通用户配置。

## 桌面开发与测试

默认启动为手动模式，旧 `--no-autostart` 参数保留兼容。服务进程独立持有 `service.lock`，即使启动中心异常退出，新服务也不能同时使用同一数据目录。启动中心消失时先请求正常停止，启动或下载卡住超过 30 秒后结束本次服务进程树；源码版没有离线文档时继续使用在线教程。旧版本没有此保护，升级前仍需先退出旧版。

父进程监视使用 Windows 进程句柄，避免仅按 PID 轮询；参考 [OpenProcess](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocess) 和 [WaitForSingleObject](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject)。`tests/test_desktop_lifecycle.py` 在临时数据目录覆盖手动启动、重复服务拒绝、运行中丢失启动中心和启动卡住时的回收。

### 手动检查更新

入口：**偏好设置 → 版本与更新 → 检查更新 → 查看更新说明 → 下载新版**。只在主动检查时访问公开的 GitHub Releases API，不携带账号、配置、路径或访问令牌。请求超时、限流、未发布正式版和缺少安装包会分别提示；检查在后台执行，不阻塞服务启停。下载前有确认提醒，默认浏览器负责下载，不自动安装、重启或停止任务。更新说明以纯文本显示，不执行发布内容中的 HTML。

维护者发布约定：在 `3441293738/creatorhub` 的 GitHub Releases 发布正式版，tag 为 `v0.3.0` 或 `0.3.0` 等三/四段数字版本，在说明中填写变更，并上传 `CreatorHub-Setup-0.3.0-windows-x64.exe`。将其设为最新正式版；draft/prerelease 不进入此更新通道。只有 Git tag、Actions artifact 或源码 ZIP 不等于桌面安装包。当前工作流仍只构建 artifact，不会自动公开发布 Release。

版本号按数字比较，不把 `0.10.0` 当作早于 `0.9.0`；源码开发版会明确提示不参与已安装版本比较。只接受项目自身 release 路径下名称匹配的 Windows x64 安装包，不支持用户传入下载地址。下载交给浏览器不代表下载成功，安装前需先“停止并退出”旧版。

接口依据：[GitHub Releases REST API](https://docs.github.com/en/rest/releases/releases#get-the-latest-release)。页面验收：`python tests/desktop_update_browser.py`（使用本地发布与网络错误样本，不实际下载安装包）。

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

当前本机产物为便携目录，不代表已生成或发布 Setup 安装包。托盘、系统保存对话框、干净机器首次安装仍需按发布前清单人工验收。
