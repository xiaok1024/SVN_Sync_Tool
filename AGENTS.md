# AGENTS.md

本文件是 `SVN_Sync_Tool` 仓库唯一的项目级 AI 协作入口，适用于整个仓库。它规定 AI 的维护边界与上下文路由；`README.md` 是用户行为和构建说明，源码是实际行为依据，测试负责为关键约束提供回归闭环。

后续 AI 协作围绕版本升级辅助工具的长期维护展开：BUG 修复、功能优化、新功能增加，以及 GUI/CLI 共享能力的持续收敛。

## 项目定位

这是一个跨平台小工具，用于版本升级场景中的 SVN 拉取、交叉文件覆盖、自动提交、升级清单提取与 Markdown 生成。有两个入口：

- GUI（`svn_sync_tool.py`）：Windows 主要使用方式，打包为 exe；
- 终端版（`svn_sync_cli.py`）：macOS 主要使用方式，支持交互菜单和子命令参数。

当前主要模块：

- `svn_sync_core.py`：GUI/CLI 唯一共享核心，负责 SVN 命令、凭据脱敏、SMB/UNC、锁处理、交叉文件扫描/复制和提交路径生成；
- `svn_sync_tool.py`：GUI 入口、界面线程调度、剪贴板适配，以及 `rt_*` 升级清单解析 API；
- `svn_sync_cli.py`：终端交互与参数入口，只编排共享能力；
- `svn_path_generator.py`：版本号解析、SVN 版本路径查询与排序的唯一实现，同时服务 Tab 5 和 `paths` 子命令；
- `svn_standard_file_core.py`：标准文件扫描、路径安全、覆盖与提交准备；
- `svn_standard_file_tab.py`：Tab 6 界面、确认流程与后台线程调度。

GUI 使用 `tkinter` / `ttkbootstrap`；终端版不依赖第三方包。SVN 操作统一通过系统 `svn` CLI。Windows 使用 PyInstaller 打包，macOS 直接运行终端版。发布产物位于 `outputs/`。

## 上下文与修改原则

- 默认使用中文沟通、说明变更、编写面向用户的文档、界面文案和交付说明。
- 修改前阅读 `README.md`、相关源码和对应测试；只加载与当前任务有关的模块，不要求遍历全部源码。
- 保持现有模块职责稳定。无界面业务优先进入共享核心，GUI/CLI 保持为薄适配层；不要为了维持“单文件”而复制业务逻辑，也不要在没有明显收益时扩大重构范围。
- `SyncEngine` 是 SVN、SMB、交叉文件和提交路径能力的唯一实现来源。`SvnSyncTool` 不得覆写或复制这些核心方法；`CliEngine` 只允许提供终端日志、变量和剪贴板适配。
- Tab 5 的版本查询与 URL 排序统一复用 `svn_path_generator.py` 的纯逻辑 API；不得在 GUI 或 CLI 中再次实现 XML 解析和排序。
- `StandardFileService` 承担 Tab 6 的无界面业务；`SvnStandardFileTab` 不新增独立 SVN 执行器或配置解析业务。
- `rt_*` 函数和 `RedTextHTMLParser` 负责升级清单富文本解析、红黑颜色识别与 Markdown 生成；修改这些规则时必须补充代表性 HTML/清单回归用例。
- 保持 Windows、macOS、中文路径、SVN 输出编码、SMB/UNC 和 Unicode 规范化兼容。
- 不主动安装依赖；缺少 PyInstaller、SVN CLI 或测试工具时，先说明用途、收益和缺失影响，等待用户确认。
- 不提交、推送、删除或重置用户已有改动，除非用户明确要求。

## 安全边界

- SVN/SMB 密码不得进入日志、异常、剪贴板、配置文件或发布产物。所有可显示的 SVN 命令必须经过统一脱敏；新增执行路径时必须有凭据脱敏测试。
- 扫描、解析、`svn status`、`--dry-run` 属于只读验证；文件覆盖、目录删除、强制解锁、共享挂载、`svn add` 和 `svn commit` 属于高风险操作。
- 验证阶段不得连接真实客户仓库执行写入，不得提交生产工作副本，不得写入生产共享目录。
- 普通覆盖应先预览并确认；GUI“一键覆盖”必须在扫描完成后显示结果并确认，CLI 非交互覆盖必须显式传入 `--yes`。
- 全自动流程必须先展示地址、目录、模式和提交信息并确认。checkout 模式删除已有工作副本前必须单独确认；CLI 非交互模式只能通过显式 `--yes` 授权。
- 全自动流程中任一文件覆盖失败时必须终止，不得在部分覆盖状态下继续执行 `svn add` 或 `svn commit`。
- 提交整个工作副本前应展示 `svn status` 并确认；全自动流程的一次总确认可授权其既定的拉取、覆盖和提交步骤，但不得隐含授权额外目录或额外仓库写入。
- GUI 线程不得执行 SVN、文件扫描、网络共享或大批量复制；后台线程不得直接更新 Tk 控件，必须通过队列或 `after` 回到主线程。

## 构建与验证

基础检查：

```bash
python3 -m py_compile svn_sync_tool.py svn_sync_core.py svn_path_generator.py svn_standard_file_core.py svn_standard_file_tab.py svn_sync_cli.py
python3 svn_sync_cli.py --help
python3 -m unittest discover -s tests -v
```

按变更范围追加验证：

- 修改 `svn_sync_core.py`：运行共享核心、CLI 和标准文件测试，并确认 `SvnSyncTool` 没有重新覆写共享核心方法；
- 修改 `svn_path_generator.py`：验证 GUI/CLI 共用的查询、XML 文件过滤、中文路径解码和三种排序；
- 修改 `rt_*`：验证红/黑颜色、QC 分组、版本去重和人读/AI Markdown；
- 修改 GUI：使用现有 `.venv-macos/bin/python svn_sync_tool.py` 做手工启动检查；若环境缺少 GUI 依赖，不自行安装，应说明未验证项；
- 修改 SVN、SMB 或 Windows 打包逻辑：除自动测试外，列出需要用户在真实 Windows/SVN/SMB 环境手工验证的场景。

发布约束：

- `dist/`、`build/`、`.venv-macos/`、`outputs/*.app/` 是本地构建内容，不作为主要提交对象；
- `outputs/SVN_Sync_Tool-macos-arm64.zip` 是历史产物，不再更新或删除；
- 只有用户明确要求 Windows 打包时才更新 `outputs/SVN_Sync_Tool.exe`；
- 未更新发布产物时必须在交付说明中明确说明。

## 变更交付要求

完成修改后应说明：

- 修改了哪些行为及其安全影响；
- 影响的主要文件；
- 已执行的自动检查和结果，或未能执行的原因；
- 是否更新 macOS / Windows 发布产物；
- 是否仍需用户手工验证 SVN、SMB、GUI、Windows 或打包场景。
