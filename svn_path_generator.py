# -*- coding: utf-8 -*-
"""SVN 版本号路径生成工具 - 独立 Tab 页面

功能：
- 输入 SVN 地址和版本号，生成文件路径列表
- 版本号格式：单版本(123)，逗号分割(123,456,789)
- 连续版本用连字符(123-456)，支持联合查询(123,456-789,1000)
- 文件排序：按路径排序、按版本号排序、按文件名排序
- 一键复制路径列表
"""

import os, sys, subprocess, threading, re, locale
import xml.etree.ElementTree as ET
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

# ── 复用主程序的环境变量 ──────────────────────────────────
_SYS_ENC = locale.getpreferredencoding()
_SVN_ENC = 'gbk' if _SYS_ENC.lower() in ('cp936', 'gbk', 'gb2312', 'gb18030') else 'utf-8'
IS_WINDOWS = (os.name == 'nt')

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
import shutil as _shutil
SVN_EXECUTABLE = _shutil.which("svn")
if not SVN_EXECUTABLE:
    for _svn_path in ("/opt/homebrew/bin/svn", "/usr/local/bin/svn", "/usr/bin/svn"):
        if os.path.isfile(_svn_path) and os.access(_svn_path, os.X_OK):
            SVN_EXECUTABLE = _svn_path
            break
SVN_EXECUTABLE = SVN_EXECUTABLE or "svn"


def parse_revision_spec(spec):
    """解析版本号字符串，返回排序后的版本号列表。
    
    格式：
    - 单个版本：'123' -> [123]
    - 逗号分割：'123,456,789' -> [123, 456, 789]
    - 连续版本：'123-456' -> [123, 124, ..., 456]
    - 联合查询：'123,456-789,1000' -> [123, 456, 457, ..., 789, 1000]
    
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


def run_svn_command(args, svn_user='', svn_pass='', timeout=60):
    """运行 svn 命令并返回 (returncode, stdout_text)。
    自动处理中文编码。
    """
    cmd = [SVN_EXECUTABLE, "--non-interactive",
           "--trust-server-cert-failures=unknown-ca,cn-mismatch,expired,not-yet-valid,other"]
    if svn_user:
        cmd.extend(["--username", svn_user])
        if svn_pass:
            cmd.extend(["--password", svn_pass])
        else:
            cmd.append("--no-auth-cache")
    cmd.extend(args)

    env = os.environ.copy()
    if os.name != 'nt':
        env["LANG"] = "zh_CN.UTF-8"
        env["LC_ALL"] = "zh_CN.UTF-8"
        env["LC_CTYPE"] = "zh_CN.UTF-8"

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=CREATE_NO_WINDOW
        )
        out, err = proc.communicate(timeout=timeout)
        try:
            text = out.decode(_SVN_ENC, errors='replace')
        except (UnicodeDecodeError, LookupError):
            text = out.decode('utf-8', errors='replace')
        err_text = err.decode(_SVN_ENC, errors='replace') if err else ''
        if err_text:
            text += '\n' + err_text
        return proc.returncode, text
    except subprocess.TimeoutExpired:
        return -1, "超时"
    except Exception as e:
        return -1, str(e)


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
        """启动后台线程生成路径"""
        url = self.svn_url.get().strip()
        spec = self.revision_spec.get().strip()
        
        if not url:
            messagebox.showwarning("提示", "请先输入 SVN 仓库地址")
            return
        if not spec:
            messagebox.showwarning("提示", "请先输入 SVN 版本号")
            return
        
        revisions = parse_revision_spec(spec)
        if not revisions:
            messagebox.showwarning("提示", "无法解析版本号，请检查格式")
            return
        
        self._set_ui_busy(True)
        self._clear_results()
        self.lbl_status.config(text="正在查询版本 %d ~ %d..." % (revisions[0], revisions[-1]))
        
        def run():
            try:
                results = []
                errors = []
                
                for rev in revisions:
                    rc, out = run_svn_command(
                        ["log", "--xml", "-v", "-r", str(rev), url],
                        self.svn_user.get().strip(),
                        self.svn_pass.get().strip()
                    )
                    if rc != 0:
                        errors.append("版本 %d: 查询失败 - %s" % (rev, out[:200]))
                        continue
                    
                    # 解析 XML
                    try:
                        root = ET.fromstring(out)
                        logentry = root.find(".//logentry")
                        if logentry is None:
                            errors.append("版本 %d: 未找到日志条目" % rev)
                            continue
                        paths = logentry.findall("paths/path")
                        if not paths:
                            errors.append("版本 %d: 无变更文件" % rev)
                            continue
                        
                        for p in paths:
                            path_text = p.text.strip() if p.text else ""
                            if path_text:
                                results.append((path_text, rev))
                    except ET.ParseError as e:
                        errors.append("版本 %d: XML 解析错误 - %s" % (rev, str(e)))
                
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
        
        # 排序
        sort_mode = self.sort_mode.get()
        base_url = self.svn_url.get().strip().rstrip("/")
        
        # 生成完整 URL
        urls_with_rev = []
        for path_text, rev in results:
            # 确保路径以 / 开头
            if not path_text.startswith("/"):
                path_text = "/" + path_text
            full_url = base_url + path_text + "(V" + str(rev) + ")"
            urls_with_rev.append((full_url, rev, path_text))
        
        # 排序
        if sort_mode == "按路径排序":
            urls_with_rev.sort(key=lambda x: x[0].lower())
        elif sort_mode == "按版本排序":
            urls_with_rev.sort(key=lambda x: (x[1], x[0].lower()))
        elif sort_mode == "按文件名排序":
            urls_with_rev.sort(key=lambda x: (os.path.basename(x[2]).lower(), x[0].lower()))
        
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
        self.txt_preview.config(state=tk.DISABLED)
        
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
        self.txt_preview.config(state=tk.DISABLED)
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
        self.txt_preview.config(state=tk.DISABLED)
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
