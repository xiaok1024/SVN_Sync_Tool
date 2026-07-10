# AGENTS.md

本文件适用于整个 `SVN_Sync_Tool` 仓库。后续 AI 协作应围绕版本升级辅助工具的长期维护目标展开：BUG 修复、功能优化、新功能增加。

## 项目定位

这是一个跨平台小工具，用于版本升级场景中的 SVN 拉取、交叉文件覆盖、自动提交、升级清单提取与 Markdown 生成。有两个入口：

- GUI（`svn_sync_tool.py`）：Windows 主要使用方式，打包为 exe；
- 终端版（`svn_sync_cli.py`）：macOS 主要使用方式，交互菜单 + 子命令参数两种用法，复用 `svn_sync_tool.py` 的业务逻辑（通过 `CliEngine` 继承 `SvnSyncTool`，不构建界面）。

当前主要技术栈：

- Python：`svn_sync_tool.py`（GUI + 业务逻辑）、`svn_sync_cli.py`（终端入口）、`svn_path_generator.py`（Tab 5 / paths）
- GUI：`tkinter` / `ttkbootstrap`（基于 `ttk`）；两个模块的 GUI 依赖导入均已做缺失降级（`GUI_AVAILABLE`），终端版不依赖第三方包
- SVN 操作：通过系统 `svn` CLI 调用
- Windows 打包：PyInstaller（macOS 不再打包 `.app`，直接运行终端版）
- 发布产物：`outputs/`

## 工作原则

- 默认使用中文沟通、说明变更、编写面向用户的文档和界面文案。
- 修改前先阅读 `README.md`、相关源码区域和现有打包说明，不凭记忆改动流程。
- 优先保持单文件结构和现有函数风格；只有在复杂度明显上升时才拆分模块。
- 保持跨平台兼容，特别关注 Windows、macOS、中文路径、SVN 输出编码、SMB/UNC 路径差异。
- 不主动安装依赖；如果缺少 PyInstaller、SVN CLI 或测试工具，先说明用途和影响，等待用户确认。
- 不提交、推送、删除或重置用户已有改动，除非用户明确要求。

## 源码维护重点

- `rt_*` 函数和 `RedTextHTMLParser` 负责升级清单富文本解析、红黑颜色识别、Markdown 生成。
- `SvnSyncTool` 负责 GUI、SVN 流程、交叉覆盖、SMB 路径处理和用户交互。
- GUI 线程中不要执行长耗时操作；SVN、文件扫描、网络共享访问等应继续使用后台线程或安全的异步处理。
- 不要在日志、异常或产物中输出明文密码、SMB 凭据或 SVN 密码。
- 文件覆盖、SVN commit、共享目录挂载等有外部影响的操作必须保守处理，并尽量提供预览、日志和错误提示。

## 构建与验证

常用检查：

```bash
python3 -m py_compile svn_sync_tool.py svn_path_generator.py svn_sync_cli.py
python3 svn_sync_cli.py --help
```

macOS 上验证（不再打包 `.app`）：

```bash
# 终端版（主要使用方式，系统 python3 即可）
python3 svn_sync_cli.py

# 如需验证 GUI（与 Windows 同一套代码），用项目虚拟环境启动
.venv-macos/bin/python svn_sync_tool.py
```

注意：

- `dist/`、`build/`、`.venv-macos/`、`outputs/*.app/` 属于本地构建内容，不应作为主要提交对象。
- macOS 不再更新 `outputs/SVN_Sync_Tool-macos-arm64.zip`（历史产物保留，不删除）。
- Windows exe 只在用户明确要求 Windows 打包时更新。
- 涉及 SVN 真实仓库写入、`svn commit`、生产共享目录写入时，不要在验证阶段擅自执行。

## 变更交付要求

完成 BUG 修复、优化或新功能后，应说明：

- 修改了哪些行为
- 影响的主要文件
- 已执行的检查或未能执行的原因
- 是否更新了 macOS / Windows 发布产物
- 是否存在需要用户手工验证的 SVN、SMB、GUI 或打包场景
