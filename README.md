# SVN 代码同步工具 / SVN Code Sync Tool

一个跨平台（Windows / macOS）图形化工具，用于从 SVN 拉取代码、用整理好的本地目录（或网络共享）覆盖交叉文件、并自动提交变更。三步流程一键完成，提交完成后可一键复制 SVN 提交记录。

A cross-platform (Windows / macOS) GUI tool for checking out code from SVN, overwriting cross-referenced files from a local organized directory (or network share), and automatically committing changes. Complete the three-step workflow with one click.

---

## 功能 / Features

| 功能 | 说明 |
|------|------|
| **SVN 拉取** | 输入 SVN 地址（支持中文路径），选择拉取目录，支持用户名/密码认证或缓存认证 |
| **交叉覆盖** | 遍历 SVN 检出目录下的每个文件，到整理好的目录中查找同名同路径文件，有则覆盖，没有则跳过 |
| **全自动流程** | 一键执行：SVN 拉取 → 交叉覆盖 → SVN 提交，实时日志输出，无需手动操作 |

| Feature | Description |
|---------|-------------|
| **SVN Checkout** | Enter SVN URL (supports Chinese characters), select checkout directory, supports username/password auth or cached auth |
| **Cross Overwrite** | Iterates every file in the SVN checkout directory, looks for matching files (same relative path) in the organized directory, overwrites if found, skips if not |
| **Auto Pipeline** | One-click execution: SVN checkout → cross-file overwrite → SVN commit, with real-time log output |

---

## 截图 / Screenshot

![SVN Sync Tool](./README.assets/1782545885107.png)

*(截图未生成，运行工具即可查看界面 / Screenshot not generated, run the tool to see the interface)*

---

## 下载 / Download

直接从 outputs/ 目录获取对应平台的预编译产物：

Grab the pre-built artifact for your platform from the outputs/ directory:

| 平台 | 产物 |
|------|------|
| **Windows** | `outputs/SVN_Sync_Tool.exe` |
| **macOS (Apple Silicon)** | `outputs/SVN_Sync_Tool-macos-arm64.zip`（解压得到 `SVN_Sync_Tool.app`） |

双击运行，无需安装 Python 或任何依赖（但系统需已安装 SVN 命令行工具）。

Double-click to run. No Python or dependencies required (the SVN command-line client must be installed on the system).

---

## 使用说明 / Usage

### 标签页 1: SVN 拉取 / Tab 1: SVN Checkout

1. 输入 **SVN 仓库地址**
2. （可选）填写 **用户名** 和 **密码**，留空则使用本地 SVN 缓存认证
3. 选择 **拉取到目录**
4. 点击 **拉取代码**
5. 日志区域实时显示 svn checkout 输出

---

### 标签页 2: 交叉覆盖 / Tab 2: Cross Overwrite

1. 选择 **SVN 拉取目录**（目标，被覆盖的目录）
2. 选择 **整理好的目录**（来源，取文件的目录）——也可直接填**网络共享地址**（见下方「共享目录地址」）
3. 点击 **扫描预览** 查看哪些文件会被覆盖
4. 点击列表中的文件可切换勾选/取消
5. 点击 **覆盖选中** 执行覆盖

也可直接点击 **一键覆盖（推荐）**，扫描 + 覆盖一步完成。

---

### 标签页 3: 全自动流程 / Tab 3: Auto Pipeline

1. 填写 **用户名/密码**（可选）
2. 输入 **SVN 仓库地址**
3. 选择 **SVN 拉取目录**
4. 选择 **整理好的目录（来源）**——也可直接填**网络共享地址**（见下方「共享目录地址」）
5. 选择拉取模式：checkout（首次）或 update（已有）
6. 输入 **SVN 提交信息**
7. 点击 **▶ 一键执行**，工具将自动完成：
   ```
   SVN 拉取 → 交叉文件覆盖 → SVN 提交
   ```
8. 日志区域实时显示每一步的输出和结果

---

## 共享目录地址 / Network Share

「整理好的目录（来源）」除了本地路径，也可以直接填**网络共享地址**。工具会按操作系统自动处理，**两个平台都无需手动改写路径**：

The "organized directory (source)" accepts a **network share address** in addition to a local path. The tool resolves it automatically per platform:

| 平台 | 支持的写法 | 处理方式 |
|------|-----------|---------|
| **Windows** | `\\server\share\path` 或 `smb://server/share/path` | 转成 UNC 路径**直接访问，无需挂载、无需填 SMB 账号**（系统按需建立连接） |
| **macOS** | `smb://server/share/path` 或 `\\server\share\path` | 自动挂载共享后访问；优先复用访达已连接的挂载（含深层挂载），临时挂载在退出时自动卸载 |

- **直接粘贴原文**：来源框可直接粘贴带提示语的整段文本，例如 `标准文件请到\\192.168.7.215\...\ecology下面提取`，工具会自动剥除「标准文件请到」「下面提取」等前后缀。
- **为什么有平台差异**：Windows 原生支持把 UNC 路径当本地路径访问；macOS 必须先把 SMB 共享挂载到文件系统才能用，`smb://` 本身只是 URL，不能当路径直接打开。
- **macOS 认证（两种方式）**：
  1. 在界面的 **SMB 账号 / 密码** 框填写凭据，工具用它挂载（凭据只存内存、不写入源码或安装包、日志不打印密码）；
  2. 或先在访达按 `Cmd+K` 输入 `smb://...` 连接一次（勾选「记住密码」存入钥匙串），工具自动复用该挂载，SMB 账号框留空即可。
- **深层挂载复用**：访达可把共享的深层子目录直接挂载（如挂到 `/Volumes/ecology`）；工具会比对挂载源的完整路径正确复用，并对中文路径做 Unicode/百分号编码归一化。
- **Windows 无需填 SMB 账号**：UNC 直接访问，SMB 账号/密码框留空即可。
- 本地路径（如 `/Users/...`、`C:\work\...`）按原有方式处理，行为不变。

---

## 构建 / Build from Source

### 前置条件 / Prerequisites

- **Python 3.6+**
- **PyInstaller**
- **SVN CLI**（`svn` / `svn.exe` 需在 PATH 中；macOS 可用 Homebrew 安装：`brew install subversion`）

### 打包命令 / Build Command

**Windows**（单文件 exe）：

```bat
pip install pyinstaller

REM 打包为单文件 exe（无控制台窗口）
pyinstaller --onefile --windowed --name "SVN_Sync_Tool" svn_sync_tool.py

REM 产物在 dist\ 下，复制到 outputs\
copy dist\SVN_Sync_Tool.exe outputs\
```

**macOS**（`.app` 应用包）：

```bash
pip install pyinstaller

# 使用本地生成的 SVN_Sync_Tool.spec 打包（产出 .app）
pyinstaller SVN_Sync_Tool.spec
# 或直接从脚本打包：
# pyinstaller --windowed --name "SVN_Sync_Tool" svn_sync_tool.py

# 产物 SVN_Sync_Tool.app 在 dist/ 下，压缩到 outputs/
cd dist && zip -r ../outputs/SVN_Sync_Tool-macos-arm64.zip SVN_Sync_Tool.app
```

> `*.spec`、`build/`、`dist/` 均已在 `.gitignore` 中忽略，仓库只保留 `outputs/` 下的成品。

### 参数说明 / Arguments Explained

| 参数 | 说明 |
|------|------|
| `--onefile` | 打包为单个文件（Windows exe 常用） |
| `--windowed` | 不显示控制台窗口（GUI 程序专用） |
| `--name "SVN_Sync_Tool"` | 指定输出文件名 |
| `svn_sync_tool.py` | 入口脚本路径 |

---

## 技术栈 / Tech Stack

- **语言**: Python 3.6+
- **GUI**: tkinter / ttk
- **SVN**: 通过 subprocess 调用系统 svn CLI
- **打包**: PyInstaller（Windows 出 exe，macOS 出 .app）

---

## 项目结构 / Project Structure

```
.
├── .gitignore                          # Git 排除规则
├── svn_sync_tool.py                    # 单文件源码（GUI + SVN/共享地址处理）
├── SVN_Sync_Tool.spec                  # PyInstaller 打包配置（本地生成，已 gitignore）
├── outputs/                            # 预编译成品（纳入版本库）
│   ├── SVN_Sync_Tool.exe               #   Windows 可执行文件
│   └── SVN_Sync_Tool-macos-arm64.zip   #   macOS (Apple Silicon) 应用包压缩
├── README.assets/                      # README 截图
└── README.md                           # 本文档
```

---

## 注意事项 / Notes

- 首次使用 SVN 功能时，如果未填写用户名/密码，会使用系统 SVN 缓存的认证信息
- 覆盖操作不可撤销，建议先在标签页 2 使用"扫描预览"查看变更
- 如果 SVN 服务器使用自签名证书，工具已默认添加 --trust-server-cert-failures 参数信任常见证书问题
- 来源目录支持直接填共享地址：Windows 用 `\\server\share`，macOS 用 `smb://server/share`，详见「共享目录地址」
- macOS 上由工具临时挂载的共享会在关闭窗口时自动卸载；访达手动连接的挂载不会被卸载
- 全自动流程提交成功后会列出本次提交文件的可访问 URL，可一键复制；提交解析使用 `svn info/log --xml`，不受中文（GBK/本地化）输出影响
- 若本次运行无变更（不产生新提交），会回退导出工作副本当前版本的文件路径，方便随时复制
- 源码使用 Python 3.6 编写，兼容 3.6+ 全系列版本
