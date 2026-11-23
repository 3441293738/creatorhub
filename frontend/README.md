# 前端开发

FastAPI 直接提供 `app/web/` 下的静态资源。日常启动无需构建；修改前端源码后，在项目根目录执行：

```bash
npm ci
npm run build:ui
```

## 源码与产物

- `html/*.html`、`styles/base.css`：工作台 HTML 分区面板模板（8 个模块）与基础内联样式源码。
- `app/*.js`：工作台核心业务逻辑按功能域拆分的 16 个模块化源码文件（请求模态、平台导航、扫码登录、账号、风控、账号中心、私信、代理设置、视频号通道、分享下载、批量采集、作品监控、评论/弹幕监听、多平台发布、自动评论与任务队列）。
- `matrix/*.jsx`、`matrix/constants.js`：多平台矩阵与汇总预览组件模块（指挥概览、四源合一汇总预览库、跨平台账号矩阵与批量运维、矩阵数据对比与聚合私信、跨平台多号错峰群发）。
- `workbench.jsx`：React 组件挂载桥接、命令面板、新手引导与现有业务表单的连接。
- `agent.jsx`、`agent.css`：AI 助手浮窗组件与样式源码。
- `ui/primitives.jsx`、`ui/motion.js`：基于 Radix UI 的抽屉、标签页、菜单、自定义下拉选择器与动效原语。
- `brand.svg`：原始 Logo，独立于通用图标。
- `icons.mjs`：生成本地 Lucide 图标集合，保留已有图标 ID。
- `app/web/appearance.css`、`workbench.css`：共享样式。
- `app/web/index.html`、`app/web/app.js`、`app/web/agent.css`、`app/web/workbench.js`：随项目提供的构建产物（支持无 Node 环境直接运行），**请勿直接手工编辑产物文件**，修改 `frontend/` 下对应源码后运行 `npm run build:ui` 重新生成。

业务表单移动而不复制，保留字段 ID、事件与提交逻辑。失败保留输入，恢复连接不自动重放写操作。修改弹窗时需保持键盘焦点、内部滚动及减少动态效果的兼容。

标签页通过卡片的 `data-section` 与 `groupSpecs` 中的值绑定，不依赖卡片排列顺序。新增标签页时同步维护这两处，并声明适用的平台。
临时隐藏功能时，在对应卡片上添加 `hidden`，标签入口和内容会一起隐藏。全平台聚合私信已整合至「矩阵数据与私信」面板，「账号矩阵」保留账号矩阵、浏览器内核与代理池三个标签页。

## 界面预览维护

调整 UI 后，同步更新 README 的截图（需已安装项目 Python 依赖和 Chrome）：

```bash
npm run build:ui
python -m preview.capture_screenshots
```

截图直接使用 `app/web/` 的当前构建产物，在临时静态站点和独立的无头浏览器中完成，不启动后端、不读取真实账号，并阻断外部及未被演示接口接管的 API 请求。图片统一宽 1600 像素，长页面完整截图，抽屉保留视口尺寸；全部场景成功后才更新 `assets/screenshots/`。

演示时间固定在 `preview/capture_screenshots.py` 的 `CAPTURE_TIME`，发布预览使用 `preview/fixtures/publish-cover.svg`。新增截图时同步维护 `SCENES` 和 README 的图片入口。

在线演示由 `preview/build_preview.py` 复用相同界面资源；提交到 `main` 后由 `.github/workflows/pages-preview.yml` 更新 GitHub Pages。

## 依赖许可证

界面原语基于 Radix UI 构建，通用图标使用本地生成的 Lucide 图标集。运行时不依赖 CDN 或远程字体。

`app/web/workbench.js.LEGAL.txt` 和 `app/web/workbench-licenses.txt` 为构建依赖的许可证文件，需与构建产物一起保留。
`licenses/react-remove-scroll-bar.txt` 补充上游 npm 包中缺失的许可证。
