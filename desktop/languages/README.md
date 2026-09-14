# 安装器简体中文资源

`ChineseSimplified.isl` 来自 [Kira 维护的 Inno Setup 简体中文翻译](https://github.com/kira-96/Inno-Setup-Chinese-Simplified-Translation)，适配 Inno Setup 6.5.0+。

固定提交、原始下载地址和 SHA-256 记录在 `upstream.json`；翻译正文保持上游原样。MIT 许可保留在 `ChineseSimplified-LICENSE.txt`，构建器会把它放入客户端的 `_internal/desktop/licenses/`。

安装脚本仅启用简体中文，不依赖构建机预装的语言包，也不继承旧版英文语言设置。字体与项目自定义提示在 `desktop/installer.iss` 中设置。标准向导按钮、目录选择、磁盘空间、安装/卸载及错误消息由该语言文件提供。

升级 Inno Setup 或此翻译时，应对比编译器的 `Default.isl` 消息键和占位符，避免新增消息退回英文；编译日志中不应出现缺失翻译警告。实现依据：[Inno Setup Languages 文档](https://jrsoftware.org/ishelp/topic_languagessection.htm)。

CI 在编译前运行 `python desktop/check_installer_language.py --compiler ISCC.exe的路径`，缺少标准消息或改变参数占位符时直接报错。
