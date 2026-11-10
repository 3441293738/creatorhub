# 安装包优化与验证记录

日期：2026-11-10。对照代码基线：`c6cb0cf`。下述为提交前本地优化阶段的验证记录；后续 v0.2.7 同步 Windows/Mac 构建，提交、打包与发布结果以 GitHub Actions 和 Releases 为准。

## 本地测量

| 指标 | 原有本地产物 v0.2.5 | 新候选包 v0.2.7 |
| --- | ---: | ---: |
| 安装包压缩体积 | 126.81 MiB | 约 94.97 MiB |
| 应用展开体积 | 412.45 MiB | 约 281.91 MiB |

压缩包减少约 25%，展开体积减少约 32%。这是两个本地产物的实测对照，不是对最新线上版本体积的推断。MiB = 1,048,576 字节；具体字节数以生成的 `package-size.json` 为准。

产物：

- `dist/installer-optimized/CreatorHub-Setup-0.2.7-windows-x64.exe`
- `dist/installer-optimized/SHA256.txt`
- `dist/installer-optimized/package-size.json`
- `dist/windows-optimized/CreatorHub/`

本地候选包用于提交前验证；v0.2.7 的正式构建读取对应版本标签与 `CHANGELOG.md` 段落。

## 已落实

1. 启动工作台不等待 Chromium 下载；复用系统 Chrome 程序，不复用用户的个人浏览器 Profile。缺失的默认浏览器按需准备，具有超时、进度、取消和重试；已完成组件复用，未完成组件不承诺断点续传。
2. 小红书的系统 Chrome/CDP 要求保留，不用 Chromium 静默替换不兼容的浏览器。
3. 视频封面和元信息统一使用现有 FFmpeg；去掉 OpenCV、NumPy，不新增另一份静态 FFmpeg/ffprobe。处理带超时、像素上限、临时文件清理，支持磁盘媒体和字节输入。
4. Patchright 自带 Node 同时用于 JS 计算；补入 crypto-js 和相关许可。冻结程序的 ExecJS 使用已确认的绝对 Node 路径，避免 Windows PATHEXT 追加导致查找失败。
5. 改为明确的模块/资源清单；保留依赖版本信息及许可文件，排除科学计算与无关 GUI 框架。CI 分别检查压缩包 110 MiB、展开 330 MiB 的预算。
6. 简化 Windows 安装向导。完整包覆盖安装仅清理应用 `_internal` 下退役的 `cv2`、`numpy`、`numpy.libs`，不触及用户数据目录。原有文件增量更新、备份、校验和失败回滚保留。
7. 普通启动默认准备服务；更新器重启显式传 `--no-autostart`。兼容旧更新器没有该参数的情况：启动器先读取更新结果/待完成标记，再决定是否自动启动服务。
8. macOS 增加原生 WebKit、数据目录、打开文件夹、进程组管理、架构匹配下载、ARM/Intel CI、DMG 签名与公证脚本。配置与验收见 [MACOS.md](MACOS.md)。

## 已通过的验证

- 相关回归：199 项通过、56 个子测试通过；之后补充完整包清理合同，并复跑相关打包/平台测试（44 项、19 个子测试通过；批次间有重叠，不能相加）。
- 桌面浏览器验收：11 项通过，包含真实服务启停、主题、键盘导航、390/740 px 布局、新增进度和取消；没有 JS 页面错误。
- 冻结程序在空 PATH 下验证随包 Node、crypto-js、ExecJS、FFmpeg 封面/元信息及资源读取。
- 冻结后台两轮启动/停止/再启动、HTTP 页面、静态资源一致性通过；没有下载浏览器。
- 冻结更新器完整安装交接、中文/空格路径、互斥锁、SHA-256、重启标记通过。
- 冻结增量更新的文件复用、切换、健康检查、重启，以及健康检查失败后还原旧版本通过。
- 本地 Inno Setup 编译成功；WebView2 bootstrapper 的微软 Authenticode 签名有效。

相关本地日志在 `build/optimized-*.log`；UI 截图与结果在 `build/desktop-qa/`。测试只使用临时目录和本地样本，不使用真实账号、不执行真实安装、不写用户配置/数据库。

## 尚待发布验收

1. **Windows 原生 WebView2**：当前主机 .NET 将程序集加载位置判为网络来源，报 `Python.Runtime.Loader.Initialize` 错误。旧 v0.2.5 包及原源码环境同样复现，相同 DLL 的 SHA-256 一致。没有放宽 .NET 或系统安全策略，也没有移除 CI 的原生窗口验收。需在干净 Windows 10/11 上复验窗口、完整安装/覆盖清理、无 WebView2 的安装、正常退出和卸载保留数据。
2. **macOS**：Windows 上完成的是平台合同/脚本检查，没有产出并实测 Mac DMG。需在 ARM/Intel Mac runner 运行流水线，配置维护者的证书和公证凭据，完成真机、最低系统版本、签名及安装验收。未签名/未公证产物标为 `UNTESTED`，流水线不会直接发布 Release。
3. **全量测试基线**：本次全量运行在 1208 项通过、24 项跳过后达到 6 个失败而停止。五项已在未改动的 `c6cb0cf` 导出目录中复现；另一个 Tk 初始化失败在独立批次通过。没有删除这些断言或将失败改成跳过：
   - `test_douyin_im_response.py` 的两项历史 `.pb` 样本对照测试；
   - `test_guide_site.py::test_readme_community_uses_direct_qr_images`（后续发布准备中已补齐原有赞赏码的断言，保留图片存在性、标签和链接校验）；
   - `test_risk_api_gates.py::test_creator_check_default_contract_and_detailed_raw_error`；
   - `test_risk_pacing.py::test_background_health_respects_browser_read_mode[False-True]`。
   日志：`build/optimization-pytest.log`、`build/optimization-baseline-tests.log`。
4. 本次更新了两个与实际接口不一致的测试替身：浏览器 `close_context`，以及冻结更新器重启时的 `--no-autostart`。没有修改对应业务行为来迎合旧替身。

本地候选包不是正式发布验收通过的声明；正式发布仍需上述检查及发布者签名/许可复核。
