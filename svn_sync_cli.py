#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SVN 代码同步工具 —— 终端版（CLI）

与图形界面（svn_sync_tool.py）共用同一套业务逻辑，提供两种用法：

1. 交互模式：直接运行 `python3 svn_sync_cli.py`，在主菜单选择功能后按提示逐项输入参数
   （对应 GUI 的 6 个标签页；常用值会记住，回车即可复用上次输入）。
2. 参数模式：`python3 svn_sync_cli.py <子命令> [参数...]`，适合脚本化调用；
   在终端里漏填的必填参数会自动转为交互式提问补全，非终端环境则直接报错退出。

子命令与 GUI 标签页对应关系：
    checkout   1. SVN 拉取
    overwrite  2. 交叉覆盖
    auto       3. 全自动流程（拉取 → 覆盖 → 提交）
    extract    4. 升级清单提取
    paths      5. 版本号路径生成
    standard   6. 标准文件获取

常用值（SVN 地址、目录、用户名等，不含密码）保存在 ~/.config/svn_sync_tool/cli.json。
"""

import argparse
import atexit
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import svn_sync_tool as core
from svn_sync_core import SyncEngine
from svn_standard_file_core import StandardFileService
import svn_path_generator as pathgen

CONFIG_DIR = os.path.expanduser("~/.config/svn_sync_tool")
CONFIG_PATH = os.path.join(CONFIG_DIR, "cli.json")

SORT_KEYS = [("rev", "按版本排序"), ("path", "按路径排序"), ("name", "按文件名排序")]
SORT_LABELS = dict(SORT_KEYS)


# ═══════════════ 无界面引擎：复用共享核心，不构建 GUI ═══════════════

class _Var:
    """tk.StringVar 的无界面替身，让 GUI 类的方法在终端下可用。"""

    def __init__(self, value=""):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class CliEngine(SyncEngine):
    """组合共享核心，不加载或继承 GUI。"""

    def __init__(self):
        super().__init__()
        self.svn_url = _Var()
        self.svn_user = _Var()
        self.svn_pass = _Var()
        self.smb_user = _Var()
        self.smb_pass = _Var()
        self.checkout_dir = _Var()
        self.source_dir = _Var()
        self.target_dir = _Var()
        self._temp_mounts = []
        atexit.register(self._cleanup_temp_mounts)

    def _log(self, _widget, message):
        sys.stdout.write(message)
        sys.stdout.flush()

    def _read_clipboard_content(self):
        """终端版剪贴板读取：HTML 优先，纯文本兜底（不依赖 tk）。"""
        if core.IS_WINDOWS:
            html = core.read_clipboard_html_windows()
            if html and html.strip():
                return html, "html"
        elif core.IS_MACOS:
            html = core.read_clipboard_html_macos()
            if html and html.strip():
                return html, "html"
        return self._read_clipboard_text(), "text"

    def _read_clipboard_text(self):
        try:
            if core.IS_MACOS:
                r = subprocess.run(["pbpaste"], capture_output=True, text=True, errors="ignore")
                if r.returncode == 0:
                    return r.stdout
            elif core.IS_WINDOWS:
                r = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                                   capture_output=True, text=True, errors="ignore")
                if r.returncode == 0:
                    return r.stdout
        except Exception:
            pass
        return ""


def copy_to_clipboard(text):
    """把文本写入系统剪贴板，成功返回 True。"""
    try:
        if core.IS_MACOS:
            r = subprocess.run(["pbcopy"], input=text.encode("utf-8"))
            return r.returncode == 0
        if core.IS_WINDOWS:
            r = subprocess.run(["clip"], input=text, text=True, shell=True)
            return r.returncode == 0
    except Exception:
        pass
    return False


# ═══════════════ 常用值记忆（不保存任何密码） ═══════════════

def load_defaults():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def sanitize_text(value):
    """移除终端输入中的非法 UTF-8 代理字符，避免子进程参数和配置写入失败。"""
    if not isinstance(value, str):
        return value
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        raw = value.encode("utf-8", errors="surrogateescape")
        return raw.decode("utf-8", errors="replace")


def normalize_revision_input(value):
    """版本表达式只允许数字、逗号、连字符和空白，过滤粘贴/终端混入的隐藏字节。"""
    return re.sub(r"[^0-9,\-\s]", "", sanitize_text(value or "")).strip()


def save_defaults(defaults, **updates):
    for key, value in updates.items():
        if value:
            defaults[key] = sanitize_text(value)
    safe_defaults = {
        sanitize_text(key): sanitize_text(value) if isinstance(value, str) else value
        for key, value in defaults.items()
    }
    temp_path = None
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=CONFIG_DIR,
                                         prefix="cli.", suffix=".tmp", delete=False) as f:
            temp_path = f.name
            json.dump(safe_defaults, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp_path, CONFIG_PATH)
        defaults.clear()
        defaults.update(safe_defaults)
    except (OSError, UnicodeError, TypeError, ValueError):
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        pass
    return defaults


# ═══════════════ 终端交互基础组件 ═══════════════

def is_tty():
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(label, default="", required=False):
    hint = "（回车=%s）" % default if default else ""
    while True:
        try:
            value = sanitize_text(input("%s%s: " % (label, hint))).strip()
        except EOFError:
            value = ""
        if not value:
            value = default
        if value or not required:
            return value
        print("  该项为必填，请输入内容。")


def ask_dir(label, default="", required=False, must_exist=False):
    while True:
        value = ask(label, default, required)
        if not value:
            return value
        value = os.path.expanduser(value)
        if must_exist and not os.path.isdir(value):
            print("  目录不存在: %s" % value)
            default = ""
            continue
        return value


def ask_secret(label):
    try:
        return getpass.getpass(label + "（输入不回显）: ").strip()
    except EOFError:
        return ""


def ask_yes_no(label, default=False):
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input("%s %s: " % (label, hint)).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes", "是")


def ask_choice(label, options, default_key=None):
    """options: [(key, 说明), ...]，返回选中的 key。"""
    keys = [k for k, _ in options]
    if default_key not in keys:
        default_key = keys[0]
    print(label + "：")
    for index, (key, text) in enumerate(options, 1):
        mark = "（默认）" if key == default_key else ""
        print("  %d. %s%s" % (index, text, mark))
    while True:
        try:
            answer = input("请选择 [1-%d，回车=默认]: " % len(options)).strip()
        except EOFError:
            return default_key
        if not answer:
            return default_key
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return keys[int(answer) - 1]
        print("  无效选择，请重新输入。")


def banner(title):
    print()
    print("━" * 46)
    print(title)
    print("━" * 46)


def require_value(value, message):
    """非交互环境下缺参数直接报错退出，交互环境由调用方提问补全。"""
    if not value:
        print("错误: " + message, file=sys.stderr)
        sys.exit(2)
    return value


def write_text_file(path, text):
    path = os.path.expanduser(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    return path


def prompt_svn_auth(defaults):
    user = ask("SVN 用户名（留空使用本地缓存认证）", defaults.get("svn_user", ""))
    password = ask_secret("SVN 密码") if user else ""
    return user, password


def prompt_smb_auth(engine, source, defaults):
    """来源为共享地址且在 macOS 上时，询问 SMB 凭据（与 SVN 账号相互独立）。"""
    if not (core.IS_MACOS and engine._is_share_address(source)):
        return "", ""
    print("来源为网络共享地址；若访达已连接过该共享（钥匙串记住密码），SMB 账号可留空自动复用。")
    smb_user = ask("SMB 账号（留空复用已有挂载/钥匙串）", defaults.get("smb_user", ""))
    smb_pass = ask_secret("SMB 密码") if smb_user else ""
    return smb_user, smb_pass


# ═══════════════ 功能 1：SVN 拉取 ═══════════════

def run_checkout(engine, url, dst):
    banner("SVN 拉取")
    rc, _ = engine._run_svn(None, "checkout", url, dst)
    if rc == 0:
        print("\n--- 拉取完成 ---")
        return True
    print("\n--- 拉取失败，返回码: %d ---" % rc)
    return False


# ═══════════════ 功能 2：交叉覆盖 ═══════════════

def scan_cross_files(engine, target, source):
    """解析来源地址（含 SMB 挂载）并扫描交叉文件，返回 [(rel, src, tgt), ...]。"""
    error = engine._precheck_source(source)
    if error:
        raise RuntimeError(error)
    if not os.path.isdir(target):
        raise RuntimeError("目标目录不存在: " + target)
    resolved = engine._resolve_source_path(source, log="cli")
    if not os.path.isdir(resolved):
        raise RuntimeError("来源目录不存在: " + resolved)
    return engine._scan_cross_files(target, resolved)


def print_scan_result(entries):
    print("扫描完成，共 %d 个交叉文件：" % len(entries))
    width = len(str(len(entries)))
    for index, (rel, _src, _tgt) in enumerate(entries, 1):
        print("  [%*d] %s" % (width, index, rel))


def copy_cross_files(engine, entries):
    def report(relative, success, error):
        print("  [覆盖] " + relative if success else "  [失败] %s - %s" % (relative, error))

    copied, errors = engine._copy_cross_files(entries, on_result=report)
    message = "覆盖完成! 成功 %d 个" % len(copied)
    if errors:
        message += ", 失败 %d 个" % len(errors)
    print(message)
    return not errors


def select_entries_interactive(entries):
    """交互式选择要覆盖的文件；返回选中的子集，取消返回 None。"""
    print_scan_result(entries)
    print("回车 = 全部覆盖；输入序号可只覆盖部分（如 1,3-5）；q = 取消")
    while True:
        try:
            answer = input("请选择: ").strip().lower()
        except EOFError:
            return None
        if answer in ("q", "quit", "0"):
            return None
        if not answer or answer in ("a", "all"):
            return entries
        indexes = [i for i in pathgen.parse_revision_spec(answer) if 1 <= i <= len(entries)]
        if indexes:
            return [entries[i - 1] for i in indexes]
        print("  无法解析序号，请重新输入（示例: 1,3-5）。")


def run_overwrite(engine, target, source, dry_run=False, assume_yes=False):
    banner("交叉覆盖")
    entries = scan_cross_files(engine, target, source)
    if not entries:
        print("未发现交叉文件（来源目录中没有与目标目录同路径的文件）")
        return True
    if dry_run:
        print_scan_result(entries)
        print("（--dry-run 仅预览，未执行覆盖）")
        return True
    if assume_yes:
        print_scan_result(entries)
        selected = entries
    elif is_tty():
        selected = select_entries_interactive(entries)
        if not selected:
            print("已取消")
            return True
        if not ask_yes_no("确定覆盖 %d 个文件？此操作不可撤销" % len(selected), default=False):
            print("已取消")
            return True
    else:
        print("非交互环境执行覆盖必须加 --yes（或先用 --dry-run 预览）", file=sys.stderr)
        return False
    return copy_cross_files(engine, selected)


# ═══════════════ 功能 3：全自动流程 ═══════════════

def show_commit_urls(engine, checkout_dir, revision, do_copy):
    urls, _relative_paths = engine._get_revision_urls(checkout_dir, revision)
    if not urls:
        return
    print("\n提交文件路径（共 %d 个）：" % len(urls))
    for url in urls:
        print(url)
    text = "\n".join(urls)
    if do_copy or (is_tty() and ask_yes_no("是否复制以上路径到剪贴板？", default=True)):
        print("已复制到剪贴板" if copy_to_clipboard(text) else "复制到剪贴板失败，请手动复制")


def run_auto(engine, url, dst, source, mode, message, assume_yes=False, do_copy=False):
    banner("【步骤 1/3】SVN 拉取")
    dst_exists = os.path.isdir(dst) and os.path.isdir(os.path.join(dst, ".svn"))
    if dst_exists and mode == "update":
        print("目录已存在，执行 svn update...")
        rc, _ = engine._run_svn(None, "update", dst)
    else:
        if dst_exists:
            if not assume_yes:
                if not is_tty() or not ask_yes_no(
                        "目录已存在且为 checkout 模式，将先删除 %s 再重新拉取，继续？" % dst, default=False):
                    print("已取消（可改用 update 模式保留已有工作副本）")
                    return False
            print("目录已存在但选择 checkout 模式，先删除再拉取...")
            shutil.rmtree(dst, ignore_errors=True)
        rc, _ = engine._run_svn(None, "checkout", url, dst)
    if rc != 0:
        print("\n--- 步骤 1 失败，终止流程 ---")
        return False
    print("\n--- 步骤 1 完成 ---")

    banner("【步骤 2/3】交叉文件覆盖")
    resolved = engine._resolve_source_path(source, log="cli")
    if not os.path.isdir(resolved):
        print("来源目录不存在: %s\n--- 步骤 2 失败，终止流程 ---" % resolved)
        return False
    entries = engine._scan_cross_files(dst, resolved)
    if not entries:
        print("未找到交叉文件，跳过覆盖")
    else:
        if not copy_cross_files(engine, entries):
            print("部分文件覆盖失败，为避免提交不完整变更，终止流程")
            return False
        print("\n--- 步骤 2 完成 ---")

    banner("【步骤 3/3】SVN 提交")
    if not engine._unlock_svn_locks_before_commit(None, dst):
        print("\n--- 提交前解锁失败，终止流程 ---")
        return False
    print("检查变更状态...")
    _, status_out = engine._run_svn(None, "status", dst)
    changed = [l for l in status_out.split("\n") if l.strip() and ".svn" not in l]
    if not changed:
        print("无变更需要提交")
        last_rev = engine._get_wc_last_revision(dst)
        if last_rev:
            print("（无新增提交，导出当前版本 %d 的文件路径）" % last_rev)
            show_commit_urls(engine, dst, last_rev, do_copy)
        print("\n全自动流程结束")
        return True
    print("共 %d 个文件有变更，正在提交..." % len(changed))
    rc, commit_out = engine._run_svn(None, "commit", dst, "-m", message)
    if rc != 0:
        print("\n--- 提交失败，返回码: %d ---" % rc)
        return False
    print("\n--- 提交成功！---")
    revision = engine._parse_revision(commit_out)
    if revision:
        print("版本号: %d" % revision)
        show_commit_urls(engine, dst, revision, do_copy)
    print("\n全自动流程结束")
    return True


# ═══════════════ 功能 4：升级清单提取 ═══════════════

def extract_list_text(engine, html=None):
    """从 HTML（默认读剪贴板）提取升级清单文本，失败抛 RuntimeError。"""
    kind = "html"
    if html is None:
        print("正在读取剪贴板...")
        html, kind = engine._read_clipboard_content()
    if not html or not html.strip():
        raise RuntimeError("剪贴板没有内容，请先从网页复制带颜色的升级清单")
    lines = core.rt_extract_list_from_html(html)
    file_lines = [l for l in lines if l.startswith(core.RT_COLOR_PREFIXES)]
    if not file_lines:
        if kind == "text":
            raise RuntimeError("剪贴板只有纯文本（拿不到颜色）：请从浏览器/富文本复制带样式的升级清单")
        raise RuntimeError("未识别到 SVN 文件行：请确认清单里包含形如 https://.../svn/客户/路径(V版本) 的 URL")
    qc_count = sum(1 for l in lines if l.startswith("QC"))
    degraded = "（纯文本，红/黑均按黑处理）" if kind == "text" else ""
    print("提取完成：%d 个 QC，%d 个文件行%s" % (qc_count, len(file_lines), degraded))
    return "\n".join(lines)


def render_list(list_text, fmt):
    """按格式生成输出文本，返回 (文本, 默认文件名)。fmt: list / md / ai。"""
    if fmt == "list":
        return list_text, "upgrade-file-list.txt"
    entries, customer, raw = core.rt_parse_txt(list_text)
    if fmt == "md":
        text = core.rt_build_human_md(entries)
        print("已生成升级 Markdown（客户: %s，%d 个 QC）" % (customer, len(entries)))
        return text, "upgrade-file-list.md"
    text = core.rt_build_ai_md(entries, customer, raw)
    print("已生成 AI Markdown（客户: %s，%d 个 QC）" % (customer, len(entries)))
    return text, "upgrade-file-list-ai.md"


def output_result_interactive(text, default_name):
    """交互模式下的结果输出循环：打印 / 保存 / 复制。"""
    while True:
        action = ask_choice("结果输出方式", [
            ("print", "打印到终端"),
            ("save", "保存到文件"),
            ("copy", "复制到剪贴板"),
            ("back", "返回"),
        ], default_key="print")
        if action == "back":
            return
        if action == "print":
            print("\n" + text)
        elif action == "save":
            path = ask("保存路径", os.path.join(os.getcwd(), default_name), required=True)
            try:
                print("已保存: " + write_text_file(path, text))
            except OSError as e:
                print("保存失败: %s" % e)
        elif action == "copy":
            print("已复制到剪贴板" if copy_to_clipboard(text) else "复制到剪贴板失败")


def run_extract(engine, html=None, list_text=None, fmt="list", out=None, do_copy=False):
    banner("升级清单提取")
    if list_text is None:
        list_text = extract_list_text(engine, html)
    text, default_name = render_list(list_text, fmt)
    if out:
        print("已保存: " + write_text_file(out, text))
    else:
        print("\n" + text)
    if do_copy:
        print("已复制到剪贴板" if copy_to_clipboard(text) else "复制到剪贴板失败")
    return True


# ═══════════════ 功能 5：版本号路径生成 ═══════════════


def run_paths(url, spec, sort_key="rev", svn_user="", svn_pass="", out=None, do_copy=False):
    banner("版本号路径生成")
    print("正在查询 SVN 版本路径...")
    results, errors = pathgen.query_revision_paths(url, spec, svn_user, svn_pass)
    rows = pathgen.build_revision_url_rows(results, url, sort_key)
    urls = [row[0] for row in rows]
    if urls:
        text = "\n".join(urls)
        if out:
            print("已保存: " + write_text_file(out, text))
        else:
            print(text)
        rev_set = sorted({rev for _p, rev in results})
        print("\n共 %d 个文件（版本: %s，%s）" % (
            len(urls), ", ".join(str(r) for r in rev_set), SORT_LABELS[sort_key]))
        if do_copy or (is_tty() and ask_yes_no("是否复制以上路径到剪贴板？", default=True)):
            print("已复制到剪贴板" if copy_to_clipboard(text) else "复制到剪贴板失败，请手动复制")
    else:
        print("未找到任何变更文件")
    if errors:
        print("\n--- 错误详情（%d 个）---" % len(errors))
        for line in errors[:10]:
            print(line)
        if len(errors) > 10:
            print("... 还有 %d 个错误" % (len(errors) - 10))
    return bool(urls)


# ═══════════════ 功能 6：标准文件获取 ═══════════════

def run_standard(engine, lines, svn_url, target, mode, title, standard_path, historical_path,
                 allow_existing=True, dry_run=False, assume_yes=False, do_commit=False, do_copy=False):
    banner("标准文件获取")
    service = StandardFileService(engine)
    items, parsed, _details = service.scan(lines, svn_url, target, mode, standard_path,
                                           historical_path, allow_existing, log="cli")
    ready = [item for item in items if item.status == "待覆盖"]
    print("解析 %d 个路径，可覆盖 %d 个" % (parsed, len(ready)))
    for item in items:
        print("[%s] %s %s" % (item.status, item.rel_path, item.detail))
    if dry_run or not ready:
        return bool(items)
    if not assume_yes:
        if not is_tty() or not ask_yes_no("确认覆盖 %d 个文件？" % len(ready), default=False):
            print("已取消")
            return is_tty()
    covered, errors = service.cover(ready)
    print("覆盖完成: %d 成功, %d 失败" % (len(covered), len(errors)))
    for error in errors:
        print("[失败] " + error)
    if not covered or not do_commit:
        return bool(covered) and not errors
    labels = {item.source_label for item in covered}
    source_label = "标准文件/历史文件" if len(labels) > 1 else next(iter(labels))
    ok, output, status = service.prepare_commit(target, covered)
    if not ok:
        if output == "目标目录没有可提交的 SVN 变更":
            print("无需提交：覆盖后 SVN 未检测到内容变化（来源文件可能与目标完全相同）")
            return True
        print(output[:1000])
        return False
    print("\n即将提交整个目标 SVN 目录，当前 svn status：")
    print(status.rstrip())
    print("\n提示：未版本控制（?）文件不会自动加入，其他已修改/已登记文件会一并提交。")
    if not assume_yes:
        if not is_tty() or not ask_yes_no("确认提交以上变更？", default=False):
            print("已取消提交；文件覆盖及 svn add 状态已保留")
            return is_tty()
    ok, output, revision, urls, _rel_paths = service.commit_working_copy(
        target, "%s %s" % (title, source_label))
    print(output[:1000])
    if not ok:
        return False
    if revision:
        print("提交版本: r%d" % revision)
    if urls:
        print("\n".join(urls))
        if do_copy:
            print("已复制到剪贴板" if copy_to_clipboard("\n".join(urls)) else "复制到剪贴板失败")
    return True


# ═══════════════ 子命令处理（参数补全 + 执行） ═══════════════

def fill_or_die(value, label, defaults_key, defaults, secret=False, is_dir=False, must_exist=False):
    """参数缺失时：终端下交互补全，非终端下报错退出。"""
    if value:
        return value
    if not is_tty():
        require_value(value, "缺少参数：%s（非交互环境必须通过命令行传入，见 --help）" % label)
    if secret:
        return ask_secret(label)
    if is_dir:
        return ask_dir(label, defaults.get(defaults_key, ""), required=True, must_exist=must_exist)
    return ask(label, defaults.get(defaults_key, ""), required=True)


def resolve_password(args_user, args_password):
    """给了用户名没给密码时，终端下补问一次（可回车跳过 = 不缓存认证）。"""
    if args_user and args_password is None and is_tty():
        return ask_secret("SVN 密码（回车跳过 = 不缓存认证）")
    return args_password or ""


def cmd_checkout(args):
    defaults = load_defaults()
    engine = CliEngine()
    url = fill_or_die(args.url, "SVN 仓库地址", "svn_url", defaults)
    if args.username is not None:
        user = args.username
    elif is_tty():
        user = ask("SVN 用户名（留空使用本地缓存认证）", defaults.get("svn_user", ""))
    else:
        user = ""
    password = resolve_password(user, args.password)
    dst = fill_or_die(args.dir, "拉取到目录", "checkout_dir", defaults, is_dir=True)
    engine.svn_user.set(user or "")
    engine.svn_pass.set(password or "")
    save_defaults(defaults, svn_url=url, checkout_dir=dst, svn_user=user)
    return run_checkout(engine, url, dst)


def cmd_overwrite(args):
    defaults = load_defaults()
    engine = CliEngine()
    target = fill_or_die(args.target, "SVN 拉取目录（目标，被覆盖）", "target_dir", defaults,
                         is_dir=True, must_exist=True)
    source = fill_or_die(args.source, "整理好的目录（来源，可填 smb:// 共享地址）", "source_dir", defaults)
    smb_user, smb_pass = args.smb_user or "", args.smb_pass or ""
    if not smb_user and is_tty():
        smb_user, smb_pass = prompt_smb_auth(engine, source, defaults)
    engine.smb_user.set(smb_user)
    engine.smb_pass.set(smb_pass)
    save_defaults(defaults, target_dir=target, source_dir=source, smb_user=smb_user)
    return run_overwrite(engine, target, source, dry_run=args.dry_run, assume_yes=args.yes)


def cmd_auto(args):
    defaults = load_defaults()
    engine = CliEngine()
    url = fill_or_die(args.url, "SVN 仓库地址", "svn_url", defaults)
    dst = fill_or_die(args.dir, "SVN 拉取目录", "checkout_dir", defaults, is_dir=True)
    source = fill_or_die(args.source, "整理好的目录（来源，可填 smb:// 共享地址）", "source_dir", defaults)
    user = args.username or ""
    password = resolve_password(user, args.password)
    if not user and is_tty() and args.username is None:
        user, password = prompt_svn_auth(defaults)
    smb_user, smb_pass = args.smb_user or "", args.smb_pass or ""
    if not smb_user and is_tty():
        smb_user, smb_pass = prompt_smb_auth(engine, source, defaults)
    mode = args.mode
    if not mode:
        if is_tty():
            mode = ask_choice("拉取模式", [
                ("checkout", "checkout（首次拉取；目录已存在会先删除重拉）"),
                ("update", "update（已有工作副本则更新，推荐日常使用）"),
            ], default_key=defaults.get("mode", "checkout"))
        else:
            mode = "checkout"
    message = args.message
    if not message:
        if is_tty():
            message = ask("SVN 提交信息", defaults.get("commit_message", "自动同步代码"), required=True)
        else:
            require_value("", "缺少参数：SVN 提交信息（-m/--message）")
    engine.svn_user.set(user)
    engine.svn_pass.set(password or "")
    engine.smb_user.set(smb_user)
    engine.smb_pass.set(smb_pass)
    save_defaults(defaults, svn_url=url, checkout_dir=dst, source_dir=source,
                  svn_user=user, smb_user=smb_user, mode=mode, commit_message=message)

    if not args.yes and is_tty():
        print("\n即将执行全自动流程：拉取(%s) → 覆盖 → 提交" % mode)
        print("  SVN 地址: %s" % url)
        print("  拉取目录: %s" % dst)
        print("  来源目录: %s" % source)
        print("  提交信息: %s" % message)
        if not ask_yes_no("确认执行？", default=True):
            print("已取消")
            return True
    elif not args.yes:
        print("非交互环境执行全自动流程必须加 --yes", file=sys.stderr)
        return False
    return run_auto(engine, url, dst, source, mode, message, assume_yes=args.yes, do_copy=args.copy)


def cmd_extract(args):
    engine = CliEngine()
    html = None
    list_text = None
    if args.list:
        with open(os.path.expanduser(args.list), "r", encoding="utf-8") as f:
            list_text = f.read().strip()
    elif args.input:
        with open(os.path.expanduser(args.input), "r", encoding="utf-8", errors="replace") as f:
            html = f.read()
    fmt = {"list": "list", "md": "md", "ai-md": "ai"}[args.format]
    try:
        return run_extract(engine, html=html, list_text=list_text, fmt=fmt,
                           out=args.output, do_copy=args.copy)
    except (RuntimeError, ValueError) as e:
        print("提取/生成失败: %s" % e, file=sys.stderr)
        return False


def cmd_paths(args):
    defaults = load_defaults()
    url = fill_or_die(args.url, "SVN 仓库地址", "svn_url", defaults)
    spec = fill_or_die(args.revisions, "版本号（如 123 / 123,456 / 123 456 / 123-456）",
                       "revisions", defaults)
    user = args.username or ""
    password = resolve_password(user, args.password)
    sort_key = args.sort or defaults.get("sort", "rev")
    save_defaults(defaults, svn_url=url, svn_user=user, sort=sort_key, revisions=spec)
    try:
        return run_paths(url, spec, sort_key=sort_key, svn_user=user, svn_pass=password or "",
                         out=args.output, do_copy=args.copy)
    except RuntimeError as e:
        print("错误: %s" % e, file=sys.stderr)
        return False


def cmd_standard(args):
    defaults = load_defaults()
    engine = CliEngine()
    svn_url = fill_or_die(args.url, "客户 SVN 地址", "svn_url", defaults)
    target = fill_or_die(args.target, "目标 SVN 目录", "target_dir", defaults,
                         is_dir=True, must_exist=True)
    historical = fill_or_die(args.historical, "历史文件路径", "historical_path", defaults)
    standard = args.standard or ""
    if args.mode == "upgrade" and not standard:
        standard = fill_or_die(None, "KB 文件路径", "standard_path", defaults)
    title = fill_or_die(args.title, "任务标题", "task_title", defaults)
    user = args.username or ""
    engine.svn_user.set(user)
    engine.svn_pass.set(resolve_password(user, args.password))
    smb_user, smb_pass = args.smb_user or "", args.smb_pass or ""
    share_source = next((path for path in (standard, historical) if engine._is_share_address(path)), "")
    if share_source and not smb_user and is_tty():
        smb_user, smb_pass = prompt_smb_auth(engine, share_source, defaults)
    engine.smb_user.set(smb_user)
    engine.smb_pass.set(smb_pass)
    with open(os.path.expanduser(args.list), "r", encoding="utf-8") as handle:
        lines = [line.strip() for line in handle if line.strip()]
    save_defaults(defaults, svn_url=svn_url, target_dir=target, historical_path=historical,
                  standard_path=standard, task_title=title)
    return run_standard(engine, lines, svn_url, target, args.mode, title, standard, historical,
                        allow_existing=not args.skip_existing, dry_run=args.dry_run,
                        assume_yes=args.yes, do_commit=args.commit, do_copy=args.copy)


# ═══════════════ 交互主菜单 ═══════════════

def menu_checkout(engine, defaults):
    url = ask("SVN 仓库地址", defaults.get("svn_url", ""), required=True)
    user, password = prompt_svn_auth(defaults)
    dst = ask_dir("拉取到目录", defaults.get("checkout_dir", ""), required=True)
    engine.svn_user.set(user)
    engine.svn_pass.set(password)
    save_defaults(defaults, svn_url=url, checkout_dir=dst, svn_user=user)
    run_checkout(engine, url, dst)


def menu_overwrite(engine, defaults):
    target = ask_dir("SVN 拉取目录（目标，被覆盖）",
                     defaults.get("target_dir", defaults.get("checkout_dir", "")),
                     required=True, must_exist=True)
    source = ask("整理好的目录（来源，可填 smb:// 共享地址或本地路径）",
                 defaults.get("source_dir", ""), required=True)
    smb_user, smb_pass = prompt_smb_auth(engine, source, defaults)
    engine.smb_user.set(smb_user)
    engine.smb_pass.set(smb_pass)
    save_defaults(defaults, target_dir=target, source_dir=source, smb_user=smb_user)
    try:
        run_overwrite(engine, target, source)
    except RuntimeError as e:
        print("错误: %s" % e)


def menu_auto(engine, defaults):
    user, password = prompt_svn_auth(defaults)
    url = ask("SVN 仓库地址", defaults.get("svn_url", ""), required=True)
    dst = ask_dir("SVN 拉取目录", defaults.get("checkout_dir", ""), required=True)
    source = ask("整理好的目录（来源，可填 smb:// 共享地址或本地路径）",
                 defaults.get("source_dir", ""), required=True)
    smb_user, smb_pass = prompt_smb_auth(engine, source, defaults)
    mode = ask_choice("拉取模式", [
        ("checkout", "checkout（首次拉取；目录已存在会先删除重拉）"),
        ("update", "update（已有工作副本则更新，推荐日常使用）"),
    ], default_key=defaults.get("mode", "checkout"))
    message = ask("SVN 提交信息", defaults.get("commit_message", "自动同步代码"), required=True)
    engine.svn_user.set(user)
    engine.svn_pass.set(password)
    engine.smb_user.set(smb_user)
    engine.smb_pass.set(smb_pass)
    save_defaults(defaults, svn_url=url, checkout_dir=dst, source_dir=source,
                  svn_user=user, smb_user=smb_user, mode=mode, commit_message=message)
    print("\n即将执行全自动流程：拉取(%s) → 覆盖 → 提交" % mode)
    print("  SVN 地址: %s" % url)
    print("  拉取目录: %s" % dst)
    print("  来源目录: %s" % source)
    print("  提交信息: %s" % message)
    if not ask_yes_no("确认执行？", default=True):
        print("已取消")
        return
    try:
        run_auto(engine, url, dst, source, mode, message)
    except RuntimeError as e:
        print("错误: %s" % e)


def menu_extract(engine, _defaults):
    print("请先在浏览器中复制带颜色的升级清单（必须是富文本，纯文本会丢失红/黑颜色）。")
    if not ask_yes_no("已复制，开始读取剪贴板？", default=True):
        return
    try:
        list_text = extract_list_text(engine)
    except RuntimeError as e:
        print("提取失败: %s" % e)
        return
    print("\n" + list_text)
    print("\n提示：如需手工微调清单，可先保存为文件编辑，再用「extract --list 文件名」重新生成。")
    while True:
        action = ask_choice("后续操作", [
            ("list", "输出提取清单"),
            ("md", "生成升级 Markdown（人读）"),
            ("ai", "生成 AI Markdown"),
            ("back", "返回主菜单"),
        ], default_key="md")
        if action == "back":
            return
        try:
            text, default_name = render_list(list_text, action)
        except (RuntimeError, ValueError) as e:
            print("生成失败: %s" % e)
            continue
        output_result_interactive(text, default_name)


def menu_paths(_engine, defaults):
    url = ask("SVN 仓库地址", defaults.get("svn_url", ""), required=True)
    user, password = prompt_svn_auth(defaults)
    print("版本号格式：单版本 123 | 多个版本 123,456 或 123 456 | 连续版本 123-456 | 联合查询 123,456-789 1000")
    spec = normalize_revision_input(ask("SVN 版本号", defaults.get("revisions", ""), required=True))
    sort_key = ask_choice("排序方式", SORT_KEYS, default_key=defaults.get("sort", "rev"))
    save_defaults(defaults, svn_url=url, svn_user=user, sort=sort_key, revisions=spec)
    try:
        run_paths(url, spec, sort_key=sort_key, svn_user=user, svn_pass=password)
    except RuntimeError as e:
        print("错误: %s" % e)


def menu_standard(engine, defaults):
    mode = ask_choice("任务类型", [("upgrade", "升级任务"), ("secondev", "二开任务")], "upgrade")
    title = ask("任务标题", defaults.get("task_title", ""), required=True)
    svn_url = ask("客户 SVN 地址", defaults.get("svn_url", ""), required=True)
    target = ask_dir("目标 SVN 目录", defaults.get("target_dir", ""), required=True, must_exist=True)
    standard = ask("KB 文件路径", defaults.get("standard_path", ""), required=True) if mode == "upgrade" else ""
    historical = ask("历史文件路径", defaults.get("historical_path", ""), required=True)
    list_file = ask("文件清单文本文件", defaults.get("standard_list", ""), required=True)
    with open(os.path.expanduser(list_file), "r", encoding="utf-8") as handle:
        lines = [line.strip() for line in handle if line.strip()]
    share_source = next((path for path in (standard, historical) if engine._is_share_address(path)), "")
    smb_user, smb_pass = prompt_smb_auth(engine, share_source, defaults) if share_source else ("", "")
    user, password = prompt_svn_auth(defaults)
    engine.smb_user.set(smb_user); engine.smb_pass.set(smb_pass)
    engine.svn_user.set(user); engine.svn_pass.set(password)
    save_defaults(defaults, task_title=title, svn_url=svn_url, target_dir=target,
                  standard_path=standard, historical_path=historical, standard_list=list_file)
    run_standard(engine, lines, svn_url, target, mode, title, standard, historical,
                 do_commit=ask_yes_no("覆盖后提交 SVN？", default=False))


MENU_ITEMS = [
    ("1", "SVN 拉取", menu_checkout),
    ("2", "交叉覆盖", menu_overwrite),
    ("3", "全自动流程（拉取 → 覆盖 → 提交）", menu_auto),
    ("4", "升级清单提取", menu_extract),
    ("5", "版本号路径生成", menu_paths),
    ("6", "标准文件获取", menu_standard),
]


def interactive_menu():
    engine = CliEngine()
    print("═" * 46)
    print("  SVN 代码同步工具（终端版）")
    print("═" * 46)
    while True:
        defaults = load_defaults()
        print()
        for key, title, _fn in MENU_ITEMS:
            print(" %s. %s" % (key, title))
        print(" 0. 退出")
        try:
            choice = input("\n请选择功能 [1-6，0 退出]: ").strip().lower()
        except EOFError:
            break
        if choice in ("0", "q", "quit", "exit"):
            break
        matched = [fn for key, _t, fn in MENU_ITEMS if key == choice]
        if not matched:
            print("无效选择，请输入 0-6。")
            continue
        try:
            matched[0](engine, defaults)
        except KeyboardInterrupt:
            print("\n已取消，返回主菜单")
        except Exception as e:
            print("执行出错: %s" % e)
    print("再见！")


# ═══════════════ 参数定义与入口 ═══════════════

def build_parser():
    parser = argparse.ArgumentParser(
        prog="svn_sync_cli.py",
        description="SVN 代码同步工具（终端版）。不带子命令运行时进入交互主菜单；"
                    "带子命令时缺失的必填参数会在终端里逐项提问补全。",
        epilog="常用值（地址、目录、用户名等，不含密码）记忆在 ~/.config/svn_sync_tool/cli.json。")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("checkout", help="SVN 拉取（对应 GUI 标签页 1）")
    p.add_argument("--url", help="SVN 仓库地址")
    p.add_argument("--dir", help="拉取到目录")
    p.add_argument("--username", help="SVN 用户名（留空使用本地缓存认证）")
    p.add_argument("--password", help="SVN 密码（不建议明文写在命令里，交互模式会隐藏输入）")

    p = sub.add_parser("overwrite", help="交叉覆盖（对应 GUI 标签页 2）")
    p.add_argument("--target", help="SVN 拉取目录（目标，被覆盖）")
    p.add_argument("--source", help="整理好的目录（来源；支持 smb://、\\\\server\\share 共享地址）")
    p.add_argument("--smb-user", help="SMB 账号（仅 macOS 且来源为共享地址时需要）")
    p.add_argument("--smb-pass", help="SMB 密码")
    p.add_argument("--dry-run", action="store_true", help="只扫描并列出会被覆盖的文件，不执行覆盖")
    p.add_argument("--yes", action="store_true", help="跳过确认直接覆盖全部交叉文件（脚本化用）")

    p = sub.add_parser("auto", help="全自动流程：拉取 → 覆盖 → 提交（对应 GUI 标签页 3）")
    p.add_argument("--url", help="SVN 仓库地址")
    p.add_argument("--dir", help="SVN 拉取目录")
    p.add_argument("--source", help="整理好的目录（来源；支持共享地址）")
    p.add_argument("-m", "--message", help="SVN 提交信息")
    p.add_argument("--mode", choices=["checkout", "update"], help="拉取模式（默认 checkout）")
    p.add_argument("--username", help="SVN 用户名")
    p.add_argument("--password", help="SVN 密码")
    p.add_argument("--smb-user", help="SMB 账号")
    p.add_argument("--smb-pass", help="SMB 密码")
    p.add_argument("--yes", action="store_true", help="跳过所有确认（脚本化用；checkout 模式会直接删除已有目录）")
    p.add_argument("--copy", action="store_true", help="完成后把提交文件路径复制到剪贴板")

    p = sub.add_parser("extract", help="升级清单提取（对应 GUI 标签页 4；默认读剪贴板富文本）")
    p.add_argument("--input", help="从 HTML 文件读取（代替剪贴板）")
    p.add_argument("--list", help="从已有清单 TXT 读取（跳过提取，直接生成 Markdown）")
    p.add_argument("--format", choices=["list", "md", "ai-md"], default="list",
                   help="输出格式：list=提取清单（默认），md=升级 Markdown，ai-md=AI Markdown")
    p.add_argument("-o", "--output", help="结果保存到文件（默认打印到终端）")
    p.add_argument("--copy", action="store_true", help="结果复制到剪贴板")

    p = sub.add_parser("paths", help="版本号路径生成（对应 GUI 标签页 5）")
    p.add_argument("--url", help="SVN 仓库地址")
    p.add_argument("-r", "--revisions", help="版本号，如 123 / 123,456 / 123 456 / 123-456")
    p.add_argument("--sort", choices=["rev", "path", "name"],
                   help="排序方式：rev=按版本（默认），path=按路径，name=按文件名")
    p.add_argument("--username", help="SVN 用户名")
    p.add_argument("--password", help="SVN 密码")
    p.add_argument("-o", "--output", help="结果保存到文件（默认打印到终端）")
    p.add_argument("--copy", action="store_true", help="结果复制到剪贴板")

    p = sub.add_parser("standard", help="标准文件获取（对应 GUI 标签页 6）")
    p.add_argument("--url", help="客户 SVN 地址")
    p.add_argument("--target", help="目标 SVN 工作副本")
    p.add_argument("--mode", choices=["upgrade", "secondev"], default="upgrade", help="任务类型")
    p.add_argument("--title", help="任务标题")
    p.add_argument("--standard", help="KB 文件路径（升级任务必填）")
    p.add_argument("--historical", help="历史文件路径")
    p.add_argument("--list", required=True, help="文件清单文本文件，每行一个路径")
    p.add_argument("--username", help="SVN 用户名")
    p.add_argument("--password", help="SVN 密码")
    p.add_argument("--smb-user", help="SMB 用户名")
    p.add_argument("--smb-pass", help="SMB 密码")
    p.add_argument("--skip-existing", action="store_true", help="跳过目标中已存在的文件")
    p.add_argument("--dry-run", action="store_true", help="仅扫描预览")
    p.add_argument("--yes", action="store_true", help="跳过覆盖确认")
    p.add_argument("--commit", action="store_true", help="覆盖后预览并提交整个目标 SVN 目录")
    p.add_argument("--copy", action="store_true", help="复制提交文件 URL")

    return parser


COMMAND_HANDLERS = {
    "checkout": cmd_checkout,
    "overwrite": cmd_overwrite,
    "auto": cmd_auto,
    "extract": cmd_extract,
    "paths": cmd_paths,
    "standard": cmd_standard,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        if is_tty():
            interactive_menu()
            return 0
        parser.print_help()
        return 2
    try:
        ok = COMMAND_HANDLERS[args.command](args)
    except RuntimeError as e:
        print("错误: %s" % e, file=sys.stderr)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)
