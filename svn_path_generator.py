# -*- coding: utf-8 -*-
"""SVN 版本号路径生成工具 - 独立 Tab 页面

功能：
- 输入 SVN 地址和版本号，生成文件路径列表
- 版本号格式：单版本(123)，逗号分割(123,456,789)
- 连续版本用连字符(123-456)，支持联合查询(123,456-789,1000)
- 文件排序：按路径排序、按版本号排序、按文件名排序
- 一键复制路径列表
"""

import os, sys, threading, re
import xml.etree.ElementTree as ET
from urllib.parse import unquote

from svn_sync_core import parse_svn_log_file_paths, run_svn_command
# GUI 依赖允许缺失：终端版只复用 parse_revision_spec / run_svn_command 等纯逻辑函数
try:
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox
    GUI_AVAILABLE = True
except ImportError:
    tk = None
    GUI_AVAILABLE = False

def parse_revision_spec(spec):
    """解析版本号字符串，返回排序后的版本号列表。
    
    格式：
    - 单个版本：'123' -> [123]
    - 逗号/空格分割：'123,456 789' -> [123, 456, 789]
    - 连续版本：'123-456' -> [123, 124, ..., 456]
    - 联合查询：'123,456-789 1000' -> [123, 456, 457, ..., 789, 1000]
    
    返回排序去重后的版本号列表。
    """
    if not spec or not spec.strip():
        return []
    spec = spec.strip().replace("，", ",").replace("－", "-").replace("–", "-").replace("—", "-")
    parts = re.split(r'[,\s]+', spec)
    revisions = set()
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            sub_parts = part.split('-', 1)
            try:
                start = int(sub_parts[0].strip())
                end = int(sub_parts[1].strip())
                if start > end:
                    start, end = end, start
                revisions.update(range(start, end + 1))
            except ValueError:
                continue
        else:
            try:
                revisions.add(int(part))
            except ValueError:
                continue
    return sorted(revisions)


def query_revision_paths(url, spec, svn_user="", svn_pass=""):
    """按版本号查询变更文件，返回 ``([(path, rev)], [错误信息])``。"""
    revisions = parse_revision_spec(spec)
    if not revisions:
        raise RuntimeError("无法解析版本号，请检查格式（示例: 123 / 123,456 / 123 456 / 123-456）")
    results, errors = [], []
    for revision in revisions:
        rc, output = run_svn_command(
            ["log", "--xml", "-v", "-r", str(revision), url], svn_user, svn_pass)
        if rc != 0:
            errors.append("版本 %d: 查询失败 - %s" % (revision, output[:200].strip()))
            continue
        try:
            paths = parse_svn_log_file_paths(output)
        except ET.ParseError as exc:
            errors.append("版本 %d: XML 解析错误 - %s" % (revision, exc))
            continue
        if not paths:
            errors.append("版本 %d: 无变更文件" % revision)
            continue
        results.extend((path, revision) for path in paths)
    return results, errors


def build_revision_url_rows(results, base_url, sort_key="rev"):
    """生成并排序 ``(完整 URL, 版本号, 仓库路径)``。"""
    sort_key = {
        "按版本排序": "rev",
        "按路径排序": "path",
        "按文件名排序": "name",
    }.get(sort_key, sort_key)
    base_url = base_url.rstrip("/")
    rows = []
    for path_text, revision in results:
        decoded_path = unquote(path_text, encoding="utf-8", errors="replace")
        if not decoded_path.startswith("/"):
            decoded_path = "/" + decoded_path
        rows.append(("%s%s(V%d)" % (base_url, decoded_path, revision), revision, decoded_path))
    if sort_key == "path":
        rows.sort(key=lambda row: row[0].lower())
    elif sort_key == "name":
        rows.sort(key=lambda row: (row[2].rsplit("/", 1)[-1].lower(), row[0].lower()))
    else:
        rows.sort(key=lambda row: (row[1], row[0].lower()))
    return rows


class SvnPathGeneratorTab:
    """SVN 版本号路径生成 Tab
    
    用法：
        tab = SvnPathGeneratorTab(parent_frame)
        tab.build()
    """

    def __init__(self, parent):
        self.parent = parent
        self.svn_url = tk.StringVar()
        self.svn_user = tk.StringVar()
        self.svn_pass = tk.StringVar()
        self.revision_spec = tk.StringVar()
        self.sort_mode = tk.StringVar(value="按版本排序")  # path / rev / filename
        
        self.path_tree = None
        self.lbl_status = None
        self.lbl_count = None
        self.btn_copy = None
        self.txt_preview = None
        self.btn_generate = None
        
        self._generated_results = []  # [(url, rev)]
        self._generated_urls = []     # [url]

    def build(self):
        """构建 Tab 页面 UI"""
        row = 0
        
        # ── SVN 地址 ──
        ttk.Label(self.parent, text="SVN 仓库地址：", font=("Microsoft YaHei", 10))\
            .grid(row=row, column=0, sticky=tk.W, pady=(0, 4))
        row += 1
        ttk.Entry(self.parent, textvariable=self.svn_url, font=("Microsoft YaHei", 10))\
            .grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        row += 1
        
        # ── SVN 账号 ──
        af = ttk.Frame(self.parent)
        af.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        ttk.Label(af, text="用户名：", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        ttk.Entry(af, textvariable=self.svn_user, font=("Microsoft YaHei", 9), width=18).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(af, text="密码：", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        ttk.Entry(af, textvariable=self.svn_pass, font=("Microsoft YaHei", 9), width=18, show="*").pack(side=tk.LEFT, padx=(0, 0))
        ttk.Label(af, text="（留空使用缓存）", font=("Microsoft YaHei", 8)).pack(side=tk.LEFT, padx=(6, 0))
        row += 1
        
        # ── 版本号输入 ──
        ttk.Label(self.parent, text="SVN 版本号：", font=("Microsoft YaHei", 10))\
            .grid(row=row, column=0, sticky=tk.W, pady=(0, 2))
        row += 1
        
        rev_frame = ttk.Frame(self.parent)
        rev_frame.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 4))
        self.rev_entry = ttk.Entry(rev_frame, textvariable=self.revision_spec, font=("Microsoft YaHei", 10))
        self.rev_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        row += 1
        
        # 格式提示
        ttk.Label(self.parent,
                  text="格式：单版本 123  |  多个版本 123,456,789  |  连续版本 123-456  |  联合查询 123,456-789,1000",
                  font=("Microsoft YaHei", 8), foreground="#888888")\
            .grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(0, 6))
        row += 1
        
        # ── 排序模式 + 操作按钮 ──
        op_frame = ttk.Frame(self.parent)
        op_frame.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        
        ttk.Label(op_frame, text="排序方式：", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        sort_combo = ttk.Combobox(op_frame, textvariable=self.sort_mode,
                                   values=["按路径排序", "按版本排序", "按文件名排序"],
                                   state="readonly", width=10, font=("Microsoft YaHei", 9))
        sort_combo.pack(side=tk.LEFT, padx=(0, 12))
        # 添加 tooltip 提示
        sort_tips = {"按路径排序": "按文件路径排序", "按版本排序": "按版本号排序", "按文件名排序": "按文件名排序"}
        def _on_sort_change(*args):
            self.sort_combo_tip = sort_tips.get(self.sort_mode.get(), "")
        # Python 3.13/Tcl 9 移除了旧式 trace("w")，优先用 trace_add
        if hasattr(self.sort_mode, "trace_add"):
            self.sort_mode.trace_add("write", _on_sort_change)
        else:
            self.sort_mode.trace("w", _on_sort_change)
        
        ttk.Label(op_frame, text="  |  ", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        
        self.btn_generate = ttk.Button(op_frame, text="生成路径", command=self._start_generate)
        self.btn_generate.pack(side=tk.LEFT, padx=(0, 8))
        
        self.btn_copy = ttk.Button(op_frame, text="复制结果", command=self._copy_paths, state=tk.DISABLED)
        self.btn_copy.pack(side=tk.LEFT, padx=(0, 8))
        
        self.btn_clear = ttk.Button(op_frame, text="清空结果", command=self._clear_results)
        self.btn_clear.pack(side=tk.LEFT)

        
        row += 1
        
        # ── 结果预览 ├──
        ttk.Label(self.parent, text="生成的文件路径：", font=("Microsoft YaHei", 9))\
            .grid(row=row, column=0, sticky=tk.W, pady=(0, 2))
        row += 1
        
        self.txt_preview = scrolledtext.ScrolledText(
            self.parent, height=16, font=("Microsoft YaHei", 9),
            bg="#1e1e1e", fg="#a0d0a0", insertbackground="white", wrap=tk.NONE
        )
        self.txt_preview.grid(row=row, column=0, columnspan=3, sticky=tk.NSEW, pady=(0, 4))
        row += 1
        
        # ── 状态栏 ──
        status_frame = ttk.Frame(self.parent)
        status_frame.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(4, 0))
        self.lbl_status = ttk.Label(status_frame, text="就绪", font=("Microsoft YaHei", 9))
        self.lbl_status.pack(side=tk.LEFT)
        self.lbl_count = ttk.Label(status_frame, text="", font=("Microsoft YaHei", 9), foreground="#666666")
        self.lbl_count.pack(side=tk.LEFT, padx=(12, 0))
        
        # ── 布局权重 ──
        self.parent.columnconfigure(1, weight=1)
        self.parent.rowconfigure(row - 2, weight=1)  # txt_preview 行

    def _start_generate(self):
        """生成并排序文件路径
        如果填了 SVN 地址+版本号，则从服务器查询；
        否则从下方文本框读取已有内容进行排序。
        """
        url = self.svn_url.get().strip()
        spec = self.revision_spec.get().strip()
        
        # 模式1: SVN 查询模式
        if url and spec:
            self._generate_from_svn(url, spec)
            return
        
        # 模式2: 本地排序模式
        self._sort_local_content()

    def _sort_local_content(self):
        """从文本框读取内容进行本地排序"""
        content = self.txt_preview.get("1.0", tk.END).strip()
        if not content:
            messagebox.showwarning("提示", "请先在下方文本框中输入或粘贴 SVN 文件路径")
            return
        
        lines = [l.strip() for l in content.split("\n") if l.strip()]
        if not lines:
            messagebox.showwarning("提示", "无有效的文件路径")
            return
        
        sort_mode = self.sort_mode.get()
        
        # 解析每行为 (url, revision, filename) 元组
        parsed = []
        for line in lines:
            rev = 0
            text = line
            # 尝试提取 (Vxxx) 格式的版本号
            m = re.search(r'\(V(\d+)\)', text)
            if m:
                rev = int(m.group(1))
                text = text[:m.start()] + text[m.end():]
            filename = os.path.basename(text.rstrip("/"))
            parsed.append((line, rev, filename))
        
        # 排序
        if sort_mode == "按路径排序":
            parsed.sort(key=lambda x: x[0].lower())
        elif sort_mode == "按版本排序":
            parsed.sort(key=lambda x: (x[1], x[0].lower()))
        elif sort_mode == "按文件名排序":
            parsed.sort(key=lambda x: (x[2].lower(), x[0].lower()))
        
        sorted_lines = [p[0] for p in parsed]
        
        self._generated_urls = sorted_lines
        self._generated_results = [(p[0], p[1]) for p in parsed]
        
        self.txt_preview.config(state=tk.NORMAL)
        self.txt_preview.delete(1.0, tk.END)
        self.txt_preview.insert(tk.END, "\n".join(sorted_lines))
        
        self.lbl_status.config(text="排序完成")
        self.lbl_count.config(text="共 %d 条路径（排序: %s）" % (len(sorted_lines), sort_mode))
        self.btn_copy.config(state=tk.NORMAL)

    def _generate_from_svn(self, url, spec):
        """从 SVN 服务器查询版本的文件路径，再按选择的排序方式排列"""
        revisions = parse_revision_spec(spec)
        if not revisions:
            messagebox.showwarning("提示", "无法解析版本号，请检查格式")
            return
        
        self._set_ui_busy(True)
        self._clear_results()
        self.lbl_status.config(text="正在查询版本 %d ~ %d..." % (revisions[0], revisions[-1]))
        svn_user = self.svn_user.get().strip()
        svn_pass = self.svn_pass.get().strip()
        
        def run():
            try:
                results, errors = query_revision_paths(url, spec, svn_user, svn_pass)
                self.root.after(0, lambda: self._display_results(results, errors))
            except Exception as e:
                self.root.after(0, lambda: self._display_error(str(e)))
        
        self.root = self.parent.winfo_toplevel()
        threading.Thread(target=run, daemon=True).start()


    def _display_results(self, results, errors):
        """显示生成结果"""
        if not results and not errors:
            self.lbl_status.config(text="未找到任何变更文件")
            self._set_ui_busy(False)
            return
        
        sort_mode = self.sort_mode.get()
        base_url = self.svn_url.get().strip().rstrip("/")
        urls_with_rev = build_revision_url_rows(results, base_url, sort_mode)
        
        self._generated_results = urls_with_rev
        self._generated_urls = [u[0] for u in urls_with_rev]
        
        # 显示
        # 合并所有显示内容，避免多次insert产生空行
        display_lines = [u for u in self._generated_urls if u.strip()]
        if errors:
            display_lines.append("")
            display_lines.append("--- 错误详情 ---")
            display_lines.extend(errors[:10])
            if len(errors) > 10:
                display_lines.append("... 还有 %d 个错误" % (len(errors) - 10))
        self.txt_preview.config(state=tk.NORMAL)
        self.txt_preview.delete(1.0, tk.END)
        self.txt_preview.insert(tk.END, "\n".join(display_lines).rstrip("\n"))
        
        # 状态
        status_parts = []
        if urls_with_rev:
            rev_set = sorted(set(u[1] for u in urls_with_rev))
            status_parts.append("共 %d 个文件（版本: %s）" % (len(urls_with_rev), ", ".join(str(r) for r in rev_set)))
        if errors:
            status_parts.append("错误: %d 个" % len(errors))
        
        self.lbl_status.config(text=" | ".join(status_parts) if status_parts else "无结果")
        self.lbl_count.config(text="共 %d 个路径（排序: %s）" % (len(self._generated_urls), sort_mode))
        self.btn_copy.config(state=tk.NORMAL if self._generated_urls else tk.DISABLED)
        self._set_ui_busy(False)

    def _display_error(self, err_msg):
        """显示错误"""
        self.txt_preview.config(state=tk.NORMAL)
        self.txt_preview.delete(1.0, tk.END)
        self.txt_preview.insert(tk.END, "错误: " + err_msg)
        self.lbl_status.config(text="查询失败")
        self.btn_copy.config(state=tk.DISABLED)
        self._set_ui_busy(False)

    def _copy_paths(self):
        """复制路径到剪贴板"""
        if not self._generated_urls:
            return
        text = "\n".join(self._generated_urls)
        self.parent.winfo_toplevel().clipboard_clear()
        self.parent.winfo_toplevel().clipboard_append(text)
        self.btn_copy.config(text="[OK] 已复制!")
        self.parent.after(2000, lambda: self.btn_copy.config(text="复制结果"))

    def _clear_results(self):
        """清空结果"""
        self._generated_results = []
        self._generated_urls = []
        self.txt_preview.config(state=tk.NORMAL)
        self.txt_preview.delete(1.0, tk.END)
        self.lbl_status.config(text="就绪")
        self.lbl_count.config(text="")
        self.btn_copy.config(state=tk.DISABLED, text="复制结果")

    def _set_ui_busy(self, busy):
        """设置 UI 忙碌状态"""
        state = tk.DISABLED if busy else tk.NORMAL
        if self.btn_generate:
            self.btn_generate.config(state=state, text="正在查询..." if busy else "排序生成")


# ── 独立运行测试 ──
if __name__ == "__main__":
    if not GUI_AVAILABLE:
        sys.stderr.write("缺少 GUI 依赖（tkinter），无法启动图形界面\n")
        sys.exit(1)
    root = tk.Tk()
    root.title("SVN 版本号路径生成工具")
    root.geometry("800x700")
    root.minsize(600, 500)
    
    style = ttk.Style()
    style.theme_use("vista" if "vista" in style.theme_names() else "clam")
    
    frame = ttk.Frame(root, padding=12)
    frame.pack(fill=tk.BOTH, expand=True)
    
    app = SvnPathGeneratorTab(frame)
    app.build()
    
    root.mainloop()
