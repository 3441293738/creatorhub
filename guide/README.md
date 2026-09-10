# CreatorHub 公开使用指南

公开用户文档源文件在 `guide/`，与被 Git 忽略的内部 `docs/` 分开。

- `index.html`：中文步骤、平台差异、截图引用和故障排查。无需 JavaScript 即可阅读与锚点跳转。
- `guide.css`：桌面目录、手机布局、键盘焦点和打印样式，无外部 CDN 或字体依赖。
- `platforms.json`：抖音、小红书、快手、视频号各自的入门路线和功能步骤，构建生成四个平台页面；通用教程保留原锚点。
- 截图沿用 `assets/screenshots/` 的脱敏示例，由预览构建脚本按清单复制。

## 本地构建与阅读

在仓库根目录运行（可使用项目虚拟环境 Python）：

```sh
python preview/build_preview.py _site
python -m http.server 8081 --bind 127.0.0.1 --directory _site
```

打开 `http://127.0.0.1:8081/guide/`。根目录仍是在线演示，`/community/` 仍是交流群入口。

## 发布

沿用 `.github/workflows/pages-preview.yml`，不要再建一个会覆盖相同 Pages 站点的部署工作流。
将改动提交并推送到 `main` 后，文档、截图和构建脚本的相关变更会触发构建部署；也可手动运行该工作流。
GitHub 仓库 Settings → Pages 的构建来源应选择 GitHub Actions。
部署成功后的指南地址为 `https://3441293738.github.io/creatorhub/guide/`。
Fork 用户需按自己的 Pages 地址调整 README 入口和指南里的仓库链接。

## 更新约定

1. 功能变化时同时检查入口名称、适用平台、前置条件、操作步骤、完成标志和失败处理。
2. 新增章节使用稳定且唯一的 `id`，同步更新目录，尽量保留原锚点以免旧链接失效。
3. 截图只使用脱敏示例。新增图片需同步 `preview/build_preview.py` 的 `GUIDE_IMAGES` 清单。
4. 不把 `docs/`、`config.yaml`、数据库、日志、账号 Profile 或运行时数据打包到公开站点。
5. 运行 `python -m unittest discover -s tests -p test_guide_site.py`，再构建检查输出。
