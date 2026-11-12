# macOS 构建与安装验收

## 状态与范围

- 与 Windows 共用 Python 业务和 React 启动中心，使用系统 WKWebView。
- 单独构建 `macos-arm64` 与 `macos-x86_64`，避免让每位用户下载两套原生依赖。
- 用户数据：`~/Library/Application Support/CreatorHub/user-data`；不写入 `.app`。
- `.github/workflows/macos-installer.yml` 与 Windows 一起响应 `v*` 版本标签，从同一提交构建同版本 ARM/Intel 包；也支持手动构建。当前只上传 Actions artifacts，不自动发布 Mac Release 资产。
- 2026-11-10 已在 GitHub macOS 15 ARM/Intel runner 完成两架构原生构建、WKWebView 页面导航、随包 Node/FFmpeg 与本地服务冒烟检查，见[云端验证构建](https://github.com/3441293738/creatorhub/actions/runs/42768658541)。实际安装、托盘、最低系统版本、Developer ID 签名与 Apple 公证仍需后续验收。
- macOS 首版通过下载匹配架构的完整 DMG 更新，退出应用后拖入 Applications 替换。暂不运行 Windows 文件增量更新器，也不修改已签名 `.app` 内的单个文件。

## 未签名验证构建

在相应架构的 Mac 上使用 Python 3.11；构建目录包含完整应用，用户无需另装 Python/Node/FFmpeg。

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r desktop/requirements-macos.txt
npm ci
npm run build:ui
npm run build:desktop
python desktop/build_macos.py --version 0.2.7 --arch arm64
# Intel 构建机使用 --arch x86_64
```

输出到 `dist/macos-架构/`。未完成签名和公证的文件命名为 `aispace-UNTESTED-...dmg`，不作为正式下载资产，也不要求用户关闭 Gatekeeper。

## Developer ID 签名与公证

本地先导入维护者自己的 Developer ID Application 证书，并使用 `notarytool store-credentials` 创建钥匙串 profile：

```sh
export MACOS_SIGNING_IDENTITY='Developer ID Application: YOUR NAME (TEAM_ID)'
python desktop/build_macos.py --version 0.2.7 --arch arm64 --notary-profile creatorhub
```

脚本按内到外签名 Mach-O 文件、嵌套 bundle 与应用，开启 Hardened Runtime，仅添加 Node/V8 所需 JIT entitlement。随后执行资源、Node/媒体处理、WKWebView、服务生命周期冒烟测试；对应用 ZIP 公证、给 `.app` 装订票据，通过 Gatekeeper 检查后创建 DMG，再签名、公证、装订并生成 SHA-256。公证返回状态必须为 `Accepted`。

在 Actions 启用 `sign` 前配置六个 repository secrets：

- `MACOS_CERTIFICATE_P12_BASE64`
- `MACOS_CERTIFICATE_PASSWORD`
- `MACOS_SIGNING_IDENTITY`
- `APPLE_API_KEY_P8_BASE64`
- `APPLE_API_KEY_ID`
- `APPLE_API_ISSUER_ID`

手动运行时勾选 `sign` 即可使用凭据。需要版本标签构建也签名时，完成上述配置后将 repository variable `MACOS_SIGN_RELEASE` 设为 `true`；默认不签名，输出名带 `UNTESTED`。版本号从标签读取，并先验证同版本中文更新日志，避免 Windows 与 Mac 版本错位。凭据缺失时签名任务直接失败，不悄悄退回未签名正式包。

凭据仅导入临时 runner 的独立钥匙串，结束时恢复钥匙串列表并清理固定临时文件。签名辅助脚本避免将带密码的失败命令回显到日志。不要将证书或私钥提交到仓库。

正式资产命名：

- `aispace-Setup-版本-macos-arm64.dmg` + `SHA256-macos-arm64.txt`
- `aispace-Setup-版本-macos-x86_64.dmg` + `SHA256-macos-x86_64.txt`

## 发布前验收清单

1. ARM 与 Intel 分别完成 CI 原生构建，保留 `macos-dependencies.txt` 和包体积报告。
2. 干净 Mac：拖入 Applications，首次运行不要求安装 Python/Node，不触发无关 Chromium 下载。
3. 真机检查中文、深色模式、托盘、文件对话框、打开数据目录、退出后没有残留服务/浏览器。
4. 有 Chrome/无 Chrome、网络中断、取消组件准备、再次重试；小红书仍需系统稳定版 Chrome。
5. 用本地 MP4/MOV 验证封面和元信息，用专门测试账号人工验证最小登录/发布流程。
6. 更新匹配 CPU 架构，只替换应用，不覆盖配置、账号 Profile、媒体或数据库；确认签名与票据仍有效。
7. Mac 真机验收及许可检查完成后，再人工发布签名后的 DMG。Windows 原有发布流程不等待实验性 Mac 构建；未签名包从对应 Actions 运行的 artifacts 获取，不混入正式安装包。

参考：[pywebview 渲染后端](https://pywebview.flowrl.com/guide/web_engine)、[PyInstaller macOS 支持](https://pyinstaller.org/en/stable/feature-notes.html)、[Apple 公证](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)、[GitHub runner 架构](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)。
