# 前端开发

FastAPI 直接提供 `app/web/` 下的静态资源。日常启动无需构建；修改前端源码后，在项目根目录执行：

```bash
npm ci
npm run build:ui
```

## 源码与产物

- `workbench.jsx`：React 组件与现有业务表单的连接。
- `ui/primitives.jsx`、`ui/motion.js`：基于 Radix 的组件与动效。
- `brand.svg`：原始 Logo，独立于通用图标。
- `icons.mjs`：生成本地 Lucide 图标集合，保留已有图标 ID。
- `app/web/appearance.css`、`workbench.css`：共享样式。
- `app/web/workbench.js`：随项目提供的构建产物，修改源码后需重新生成。

业务表单移动而不复制，保留字段 ID、事件与提交逻辑。失败保留输入，恢复连接不自动重放写操作。修改弹窗时需保持键盘焦点、内部滚动及减少动态效果的兼容。

## 测试

```bash
python -m pytest -q
```

浏览器测试默认跳过。设置 `CREATORHUB_RUN_LOCAL_CDP=1` 后执行：

```bash
python -m pytest tests/test_web_appearance_browser.py -q
```

浏览器写操作使用隔离的示例接口，不操作真实账号。

## 依赖许可证

组件使用 shadcn/ui 的组合方式及 Radix primitives，通用图标使用 Lucide。运行时不依赖 CDN 或远程字体。

`app/web/workbench.js.LEGAL.txt` 和 `app/web/workbench-licenses.txt` 为构建依赖的许可证文件，需与构建产物一起保留。
`licenses/react-remove-scroll-bar.txt` 补充上游 npm 包中缺失的许可证。
