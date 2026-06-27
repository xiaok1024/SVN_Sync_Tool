# SVN 代码同步工具 / SVN Code Sync Tool

一个 Windows 图形化工具，用于从 SVN 拉取代码、用整理好的本地目录覆盖交叉文件、并自动提交变更。三步流程一键完成。

A Windows GUI tool for checking out code from SVN, overwriting cross-referenced files from a local organized directory, and automatically committing changes. Complete the three-step workflow with one click.

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

![SVN Sync Tool](docs/screenshot.png)

*(截图未生成，运行工具即可查看界面 / Screenshot not generated, run the tool to see the interface)*

---

## 下载 / Download

直接从 outputs/ 目录获取预编译的可执行文件：

Grab the pre-built executable from the outputs/ directory:

`
outputs/SVN_Sync_Tool.exe
`

双击运行，无需安装 Python 或任何依赖。

Double-click to run. No Python or dependencies required.

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
2. 选择 **整理好的目录**（来源，取文件的目录）
3. 点击 **扫描预览** 查看哪些文件会被覆盖
4. 点击列表中的文件可切换勾选/取消
5. 点击 **覆盖选中** 执行覆盖

也可直接点击 **一键覆盖（推荐）**，扫描 + 覆盖一步完成。

---

### 标签页 3: 全自动流程 / Tab 3: Auto Pipeline

1. 填写 **用户名/密码**（可选）
2. 输入 **SVN 仓库地址**
3. 选择 **SVN 拉取目录**
4. 选择 **整理好的目录（来源）**
5. 选择拉取模式：checkout（首次）或 update（已有）
6. 输入 **SVN 提交信息**
7. 点击 **▶ 一键执行**，工具将自动完成：
   `
   SVN 拉取 → 交叉文件覆盖 → SVN 提交
   `
8. 日志区域实时显示每一步的输出和结果

---

## 构建 / Build from Source

### 前置条件 / Prerequisites

- **Python 3.6+**
- **PyInstaller 4.x**
- **SVN CLI**（svn.exe 需在 PATH 中）

### 打包命令 / Build Command

`ash
# 安装 PyInstaller（如果未安装）
pip install pyinstaller

# 打包为单文件 exe（无控制台窗口）
pyinstaller --onefile --windowed --name "SVN_Sync_Tool" svn_sync_tool.py

# 打包完成后，exe 在 dist/ 目录下
# 可将其复制到 outputs/ 目录
copy dist\SVN_Sync_Tool.exe outputs\
`

### 参数说明 / Arguments Explained

| 参数 | 说明 |
|------|------|
| --onefile | 打包为单个 exe 文件 |
| --windowed | 不显示控制台窗口（GUI 程序专用） |
| --name "SVN_Sync_Tool" | 指定输出文件名 |
| svn_sync_tool.py | 入口脚本路径 |

---

## 技术栈 / Tech Stack

- **语言**: Python 3.6+
- **GUI**: tkinter / ttk
- **SVN**: 通过 subprocess 调用系统 svn CLI
- **打包**: PyInstaller 4.x

---

## 项目结构 / Project Structure

`
.
├── .gitignore              # Git 排除规则
├── svn_sync_tool.py        # 完整源码（456 行）
├── outputs/
│   └── SVN_Sync_Tool.exe   # 预编译可执行文件（9MB）
├── work/                   # 构建中间件（已 gitignore）
└── README.md               # 本文档
`

---

## 注意事项 / Notes

- 首次使用 SVN 功能时，如果未填写用户名/密码，会使用系统 SVN 缓存的认证信息
- 覆盖操作不可撤销，建议先在标签页 2 使用"扫描预览"查看变更
- 如果 SVN 服务器使用自签名证书，工具已默认添加 --trust-server-cert-failures 参数信任常见证书问题
- 源码使用 Python 3.6 编写，兼容 3.6+ 全系列版本
