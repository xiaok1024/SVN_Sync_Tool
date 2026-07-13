# -*- coding: utf-8 -*-
"""标准文件获取工具 - 独立 Tab 页面
功能：
- 支持升级任务 / 二开任务两种模式
- 解析 SVN URL 列表，按相对 ecology 路径到标准/历史文件目录中查找
- Dry-run 扫描预览 -> 确认覆盖 -> SVN add -> SVN commit 完整流程
- 来源路径支持本地路径、UNC、SMB
"""

import os
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
from svn_sync_core import SyncEngine
from svn_standard_file_core import StandardFileItem, StandardFileService


class SvnStandardFileTab:
    """标准文件获取 Tab"""

    def __init__(self, parent, engine=None):
        self.parent = parent
        self.engine = engine or SyncEngine()
        self.svn_user = engine.svn_user if engine else tk.StringVar()
        self.svn_pass = engine.svn_pass if engine else tk.StringVar()
        self.task_title = tk.StringVar()
        self.task_mode = tk.StringVar(value="upgrade")
        self.svn_root = tk.StringVar()
        self.target_dir = tk.StringVar()
        self.standard_path = tk.StringVar()
        self.historical_path = tk.StringVar()
        self.smb_user = engine.smb_user if engine else tk.StringVar()
        self.smb_pass = engine.smb_pass if engine else tk.StringVar()
        self.engine.svn_user = self.svn_user
        self.engine.svn_pass = self.svn_pass
        self.engine.smb_user = self.smb_user
        self.engine.smb_pass = self.smb_pass
        self.service = StandardFileService(self.engine)
        self.allow_existing = tk.BooleanVar(value=True)
        self.auto_commit = tk.BooleanVar(value=True)
        self.txt_file_list = None
        self.txt_log = None
        self.lbl_status = None
        self.btn_scan = None
        self.btn_cover = None
        self.btn_commit = None
        self.btn_copy_svn_paths = None
        self._commit_urls = []
        self._commit_rel_paths = []
        self._scan_result = []
        self._cover_done = False
        self._scan_logs = []
        self._covered_items = []

    def _log(self, msg):
        self.txt_log.config(state=tk.NORMAL)
        self.txt_log.insert(tk.END, msg + "\n")
        self.txt_log.see(tk.END)
        self.txt_log.config(state=tk.DISABLED)

    def _clear_log(self):
        self.txt_log.config(state=tk.NORMAL)
        self.txt_log.delete(1.0, tk.END)
        self.txt_log.config(state=tk.DISABLED)

    def _set_ui_busy(self, busy):
        state = tk.DISABLED if busy else tk.NORMAL
        for btn in (self.btn_scan, self.btn_cover, self.btn_commit):
            if btn:
                btn.config(state=state)
        if not busy:
            if self._scan_result:
                ready = sum(1 for r in self._scan_result if r[5].startswith("待覆盖"))
                if ready:
                    self.btn_cover.config(state=tk.NORMAL)
                covered = sum(1 for r in self._scan_result if r[5] == "已覆盖")
                if covered:
                    self.btn_commit.config(state=tk.NORMAL)
            self.btn_scan.config(state=tk.NORMAL)
        self.parent.update_idletasks()

    def _paste_from_clipboard(self):
        try:
            text = self.parent.winfo_toplevel().clipboard_get()
            self.txt_file_list.delete(1.0, tk.END)
            self.txt_file_list.insert(tk.END, text)
            self._log("已从剪贴板粘贴 %d 行" % len(text.splitlines()))
        except tk.TclError:
            messagebox.showwarning("提示", "剪贴板为空或无法读取")

    def _clear_file_list(self):
        self.txt_file_list.delete(1.0, tk.END)


    def _get_file_lines(self):
        content = self.txt_file_list.get("1.0", tk.END).strip()
        if not content:
            return []
        return [l.strip() for l in content.split("\n") if l.strip()]

    def _validate_scan(self):
        errors = []
        if not self.task_title.get().strip():
            errors.append("任务标题不能为空")
        if not self.svn_root.get().strip():
            errors.append("客户 SVN 地址不能为空")
        if not self.target_dir.get().strip():
            errors.append("目标 SVN 目录不能为空")
        elif not os.path.isdir(self.target_dir.get().strip()):
            errors.append("目标 SVN 目录不存在")
        if self.task_mode.get() == "upgrade" and not self.standard_path.get().strip():
            errors.append("升级任务需要填写标准文件路径")
        if not self.historical_path.get().strip():
            errors.append("历史文件路径不能为空")
        if not self._get_file_lines():
            errors.append("文件清单为空")
        return errors

    def _run_scan(self, params=None):
        """执行扫描（后台线程）"""
        if params is None:
            params = (self._get_file_lines(), self.svn_root.get().strip(), self.target_dir.get().strip(),
                      self.task_mode.get(), self.standard_path.get().strip(),
                      self.historical_path.get().strip(), self.allow_existing.get())
        lines, svn_root, target_dir, task_mode, standard_path, historical_path, allow_existing = params
        items, parsed_count, details = self.service.scan(
            lines, svn_root, target_dir, task_mode, standard_path, historical_path, allow_existing)
        self._scan_logs = details
        results = [(i.rel_path, i.source_file, i.source_label, i.target_file,
                    i.target_exists, i.status, i.detail) for i in items]
        return results, parsed_count

    def _start_scan(self):
        errors = self._validate_scan()
        if errors:
            messagebox.showwarning("参数检查", "\n".join(errors))
            return
        self._set_ui_busy(True)
        self._clear_log()
        self._log("正在扫描...")
        self.lbl_status.config(text="扫描中...")
        self._commit_urls = []
        self._commit_rel_paths = []
        self._scan_result = []
        self._cover_done = False
        self._scan_logs = []
        params = (self._get_file_lines(), self.svn_root.get().strip(), self.target_dir.get().strip(),
                  self.task_mode.get(), self.standard_path.get().strip(),
                  self.historical_path.get().strip(), self.allow_existing.get())

        def run():
            try:
                results, parsed_count = self._run_scan(params)
                self.parent.after(0, lambda: self._display_scan(results, parsed_count))
            except Exception as e:
                self.parent.after(0, lambda: self._log("扫描异常: %s" % e))
                self.parent.after(0, lambda: self._set_ui_busy(False))
        threading.Thread(target=run, daemon=True).start()

    def _display_scan(self, results, parsed_count):
        self._scan_result = results
        ready_count = sum(1 for r in results if r[5].startswith("待覆盖"))
        skip_count = sum(1 for r in results if "跳过" in r[5])
        missing_count = sum(1 for r in results if "未找到" in r[5])
        self._log("解析了 %d 个文件路径" % parsed_count)
        self._log("可覆盖: %d | 跳过: %d | 未找到来源: %d" % (ready_count, skip_count, missing_count))
        self._log("-" * 60)
        if self._scan_logs:
            self._log("来源查找详情：")
            for log in self._scan_logs:
                self._log(log)
            self._log("-" * 60)
        for r in results:
            self._log("[%s] %s  %s" % (r[5], r[0], r[6]))
        self._log("-" * 60)
        if ready_count > 0:
            self.btn_cover.config(state=tk.NORMAL)
            self.lbl_status.config(text="扫描完成：%d 个待覆盖, %d 个缺失" % (ready_count, missing_count), foreground="#cc6600")
        elif missing_count > 0:
            self.lbl_status.config(text="扫描完成：全部缺失 (%d)，请检查来源路径" % missing_count, foreground="#cc4444")
        else:
            self.lbl_status.config(text="扫描完成：无待覆盖项", foreground="#338833")
        self._set_ui_busy(False)

    def _start_cover(self):
        if not self._scan_result:
            messagebox.showwarning("提示", "请先执行扫描预览")
            return
        ready = [r for r in self._scan_result if r[5].startswith("待覆盖")]
        if not ready:
            messagebox.showinfo("提示", "没有待覆盖的文件")
            return
        if not messagebox.askyesno("确认覆盖", "确定覆盖 %d 个文件到目标 SVN 目录？\n覆盖后可通过 SVN 还原" % len(ready)):
            return
        self._set_ui_busy(True)
        self._log("\n开始覆盖...")

        def run():
            try:
                covered, errors = self._run_cover(ready)
                self.parent.after(0, lambda: self._display_cover_result(covered, errors))
            except Exception as e:
                self.parent.after(0, lambda: self._log("覆盖异常: %s" % e))
                self.parent.after(0, lambda: self._set_ui_busy(False))
        threading.Thread(target=run, daemon=True).start()

    def _run_cover(self, ready_items):
        items = [StandardFileItem(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in ready_items]
        return self.service.cover(items)

    def _display_cover_result(self, covered, errors):
        self._covered_items = covered
        for item in covered:
            self._log("ok %s <- %s" % (item.rel_path, item.source_label))
        if errors:
            for e in errors:
                self._log("fail %s" % e)
        self._log("覆盖完成: %d 成功, %d 失败" % (len(covered), len(errors)))
        new_results = []
        for r in self._scan_result:
            rel_path, src_file, src_label2, tgt, de, status, detail = r
            if status.startswith("待覆盖") and any(c.rel_path == rel_path for c in covered):
                new_results.append((rel_path, src_file, src_label2, tgt, True, "已覆盖", "<- %s" % src_label2))
            else:
                new_results.append(r)
        self._scan_result = new_results
        self._cover_done = True
        if self.auto_commit.get() and covered:
            self._log("\n-> 自动提交 SVN...")
            self._start_commit()
        else:
            if covered:
                self.btn_commit.config(state=tk.NORMAL)
                self.lbl_status.config(text="覆盖完成：%d 个文件，点击提交 SVN" % len(covered), foreground="#338833")
            self._set_ui_busy(False)

    def _start_commit(self):
        target_dir = self.target_dir.get().strip()
        if not target_dir or not os.path.isdir(target_dir):
            messagebox.showwarning("提示", "目标 SVN 目录无效")
            self._set_ui_busy(False)
            return
        title = self.task_title.get().strip()
        source_types = {item.source_label for item in self._covered_items}
        if "标准文件" in source_types and "历史文件" in source_types:
            source_label = "标准文件/历史文件"
        elif "历史文件" in source_types:
            source_label = "历史文件"
        else:
            source_label = "标准文件"
        commit_msg = ("%s %s" % (title, source_label)) if title else source_label
        self._log("提交信息: %s" % commit_msg)
        self._set_ui_busy(True)

        def run():
            try:
                ok, out, status = self.service.prepare_commit(target_dir, self._covered_items)
                self.parent.after(0, lambda o=ok, a=out, s=status:
                                  self._confirm_working_copy_commit(o, a, s, target_dir, commit_msg))
            except Exception as e:
                self.parent.after(0, lambda err=e: self._log("SVN 异常: %s" % err))
                self.parent.after(0, lambda: self._set_ui_busy(False))
        threading.Thread(target=run, daemon=True).start()

    def _confirm_working_copy_commit(self, ok, output, status, target_dir, commit_msg):
        if not ok:
            if output == "目标目录没有可提交的 SVN 变更":
                self._log("无需提交：覆盖后 SVN 未检测到内容变化（来源文件可能与目标完全相同）")
                self.lbl_status.config(text="无需提交：没有 SVN 内容变化", foreground="#338833")
            else:
                self._log("提交准备失败: %s" % output[:1000])
                self.lbl_status.config(text="SVN 提交准备失败", foreground="#cc4444")
            self._set_ui_busy(False)
            return
        if output.strip():
            self._log("svn add 结果：")
            self._log(output.rstrip())
        self._log("-" * 60)
        self._log("即将提交整个目标 SVN 目录，待提交状态：")
        self._log(status.rstrip())
        self._log("-" * 60)
        confirmed = messagebox.askyesno(
            "确认 SVN 提交",
            "为兼容新增目录，本次将提交整个目标 SVN 目录。\n\n"
            "未版本控制（?）文件不会自动加入，但其他已修改/已登记文件会一并提交。\n"
            "完整 svn status 已输出到执行日志。\n\n确认继续提交？",
            parent=self.parent.winfo_toplevel())
        if not confirmed:
            self._log("用户取消提交；文件覆盖及 svn add 状态已保留")
            self.lbl_status.config(text="已取消 SVN 提交", foreground="#cc6600")
            self._set_ui_busy(False)
            return
        self._log("用户已确认，正在提交整个目标 SVN 目录...")

        def run():
            try:
                ok2, out, rev, urls, rel_paths = self.service.commit_working_copy(target_dir, commit_msg)
                self.parent.after(0, lambda o=ok2, text=out, r=rev, u=urls, rp=rel_paths:
                                  self._display_commit_result(o, text, r, u, rp))
            except Exception as e:
                self.parent.after(0, lambda err=e: self._display_commit_result(
                    False, "SVN 异常: %s" % err, None, [], []))
        threading.Thread(target=run, daemon=True).start()

    def _display_commit_result(self, ok, output, rev, urls, rel_paths):
        if ok:
            self._log("提交成功")
            self._log(output[:1000])
            self.lbl_status.config(text="SVN 提交成功", foreground="#338833")
            if rev and urls:
                self._display_commit_paths(urls, rel_paths, rev)
        else:
            self._log("提交失败: %s" % output[:1000])
            self.lbl_status.config(text="SVN 提交失败", foreground="#cc4444")
        self._set_ui_busy(False)

    def _display_commit_paths(self, urls, rel_paths, rev):
        """显示提交文件路径，并写入执行日志"""
        self._commit_urls = urls
        self._commit_rel_paths = rel_paths
        self.btn_copy_svn_paths.config(state=tk.NORMAL)
        # 写入执行日志
        self._log("-" * 60)
        self._log("提交版本: r%s" % rev)
        for url in urls:
            self._log("  " + url)
        self._log("-" * 60)

    def _copy_commit_paths(self):
        if not self._commit_urls:
            return
        text = "\n".join(self._commit_urls)
        self.parent.winfo_toplevel().clipboard_clear()
        self.parent.winfo_toplevel().clipboard_append(text)
        self.btn_copy_svn_paths.config(text="已复制")
        self.parent.after(2000, lambda: self.btn_copy_svn_paths.config(text="复制提交文件路径"))

    def build(self):

        outer = ttk.Frame(self.parent)
        outer.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)
        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")

        def _bind_wheel(event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_wheel(event):
            canvas.unbind_all("<MouseWheel>")
        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        row = 0

        row += 1
        # Section 1: 任务配置
        sec2 = ttk.LabelFrame(scroll_frame, text=" 1. 任务配置 ", padding=8)
        sec2.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8))
        sec2.columnconfigure(1, weight=1)
        r = 0
        ttk.Label(sec2, text="任务标题：").grid(row=r, column=0, sticky=tk.W, padx=(0, 6), pady=2)
        ttk.Entry(sec2, textvariable=self.task_title).grid(row=r, column=1, columnspan=2, sticky=tk.EW, pady=2)
        r += 1
        ttk.Label(sec2, text="任务类型：").grid(row=r, column=0, sticky=tk.W, padx=(0, 6), pady=2)
        tf = ttk.Frame(sec2)
        tf.grid(row=r, column=1, columnspan=2, sticky=tk.W, pady=2)
        ttk.Radiobutton(tf, text="升级任务 (upgrade)", variable=self.task_mode, value="upgrade").pack(side=tk.LEFT, padx=(0, 16))
        ttk.Radiobutton(tf, text="二开任务 (secondev)", variable=self.task_mode, value="secondev").pack(side=tk.LEFT)
        ttk.Label(tf, text="升级: 标准文件->历史文件 | 二开: 仅历史文件", foreground="#888").pack(side=tk.LEFT, padx=(10, 0))
        r += 1
        ttk.Label(sec2, text="客户 SVN 地址：").grid(row=r, column=0, sticky=tk.W, padx=(0, 6), pady=2)
        ttk.Entry(sec2, textvariable=self.svn_root, font=("Consolas", 9)).grid(row=r, column=1, columnspan=2, sticky=tk.EW, pady=2)
        r += 1

        row += 1
        # Section 2: 路径配置
        sec3 = ttk.LabelFrame(scroll_frame, text=" 2. 路径配置 ", padding=8)
        sec3.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8))
        sec3.columnconfigure(1, weight=1)
        r = 0
        ttk.Label(sec3, text="目标 SVN 目录：").grid(row=r, column=0, sticky=tk.W, padx=(0, 6), pady=2)
        ttk.Entry(sec3, textvariable=self.target_dir).grid(row=r, column=1, sticky=tk.EW, pady=2)
        ttk.Button(sec3, text="浏览...", command=lambda: self.target_dir.set(filedialog.askdirectory())).grid(row=r, column=2, padx=(6, 0), pady=2)
        r += 1
        # 标准文件路径行——放入Frame以便二开任务时整行隐藏
        self._std_frame = ttk.Frame(sec3)
        self._std_frame.grid(row=r, column=0, columnspan=3, sticky=tk.EW, pady=2)
        self._std_frame.columnconfigure(1, weight=1)
        ttk.Label(self._std_frame, text="KB文件路径：").grid(row=0, column=0, sticky=tk.W, padx=(0, 6))
        ttk.Entry(self._std_frame, textvariable=self.standard_path).grid(row=0, column=1, sticky=tk.EW)
        ttk.Button(self._std_frame, text="浏览...", command=lambda: self.standard_path.set(filedialog.askdirectory())).grid(row=0, column=2, padx=(6, 0))
        r += 1
        ttk.Label(sec3, text="历史文件路径：").grid(row=r, column=0, sticky=tk.W, padx=(0, 6), pady=2)
        ttk.Entry(sec3, textvariable=self.historical_path).grid(row=r, column=1, sticky=tk.EW, pady=2)
        ttk.Button(sec3, text="浏览...", command=lambda: self.historical_path.set(filedialog.askdirectory())).grid(row=r, column=2, padx=(6, 0), pady=2)
        r += 1
        ttk.Label(sec3, text="支持本地路径、UNC (\\\\)、smb://，SMB 需填下方凭据", foreground="#888").grid(row=r, column=0, columnspan=3, sticky=tk.W, pady=2)

        row += 1
        # Section 3: 凭据
        sec4 = ttk.LabelFrame(scroll_frame, text=" 3. 凭据（可选） ", padding=8)
        sec4.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8))
        af = ttk.Frame(sec4)
        af.grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=2)
        ttk.Label(af, text="SMB 账号：").pack(side=tk.LEFT)
        ttk.Entry(af, textvariable=self.smb_user, width=16).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(af, text="密码：").pack(side=tk.LEFT)
        ttk.Entry(af, textvariable=self.smb_pass, width=16, show="*").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(af, text="（来源填 smb:// 时使用；本地/UNC 可留空）", foreground="#888").pack(side=tk.LEFT, padx=(6, 0))
        af2 = ttk.Frame(sec4)
        af2.grid(row=1, column=0, columnspan=3, sticky=tk.W, pady=2)
        ttk.Label(af2, text="SVN 用户：").pack(side=tk.LEFT)
        ttk.Entry(af2, textvariable=self.svn_user, width=16).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(af2, text="密码：").pack(side=tk.LEFT)
        ttk.Entry(af2, textvariable=self.svn_pass, width=16, show="*").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(af2, text="（留空使用缓存）", foreground="#888").pack(side=tk.LEFT, padx=(6, 0))

        row += 1
        # Section 4: 文件清单
        sec5 = ttk.LabelFrame(scroll_frame, text=" 4. 文件清单 ", padding=8)
        sec5.grid(row=row, column=0, columnspan=3, sticky=tk.NSEW, pady=(0, 8))
        sec5.columnconfigure(0, weight=1)
        sec5.rowconfigure(1, weight=1)
        btn_bar = ttk.Frame(sec5)
        btn_bar.grid(row=0, column=0, columnspan=2, sticky=tk.EW, pady=(0, 4))
        ttk.Label(btn_bar, text="粘贴源码路径列表（每行一个，如 src/com/api/.../DocAccService.java）：").pack(side=tk.LEFT)
        ttk.Button(btn_bar, text="从剪贴板粘贴", command=self._paste_from_clipboard).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(btn_bar, text="清空", command=self._clear_file_list).pack(side=tk.RIGHT, padx=(4, 0))
        self.txt_file_list = scrolledtext.ScrolledText(sec5, height=8, font=("Consolas", 9), wrap=tk.NONE)
        self.txt_file_list.grid(row=1, column=0, columnspan=2, sticky=tk.NSEW)

        row += 1
        # Section 5: 操作区域
        sec6 = ttk.Frame(scroll_frame)
        sec6.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8))
        ttk.Checkbutton(sec6, text="允许覆盖已存在的文件", variable=self.allow_existing).grid(row=0, column=0, padx=(0, 16), sticky=tk.W)
        ttk.Checkbutton(sec6, text="覆盖后提交 SVN", variable=self.auto_commit).grid(row=0, column=1, padx=(0, 16), sticky=tk.W)
        self.lbl_status = ttk.Label(sec6, text="就绪", foreground="#555")
        self.lbl_status.grid(row=0, column=2, sticky=tk.EW, padx=(16, 0))
        sec6.columnconfigure(2, weight=1)

        row += 1
        btn_row = ttk.Frame(scroll_frame)
        btn_row.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8))
        self.btn_scan = ttk.Button(btn_row, text="扫描预览", command=self._start_scan, width=14)
        self.btn_scan.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_cover = ttk.Button(btn_row, text="确认覆盖", command=self._start_cover, width=14, state=tk.DISABLED)
        self.btn_cover.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_commit = ttk.Button(btn_row, text="提交 SVN", command=self._start_commit, width=14, state=tk.DISABLED)
        self.btn_commit.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_copy_svn_paths = ttk.Button(btn_row, text="复制提交文件路径", command=self._copy_commit_paths, state=tk.DISABLED)
        self.btn_copy_svn_paths.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="清空日志", command=self._clear_log).pack(side=tk.RIGHT)

        row += 1
        # Section 7: 日志
        sec7 = ttk.LabelFrame(scroll_frame, text=" 执行日志 ", padding=8)
        sec7.grid(row=row, column=0, columnspan=3, sticky=tk.NSEW, pady=(0, 4))
        sec7.columnconfigure(0, weight=1)
        sec7.rowconfigure(0, weight=1)
        self.txt_log = scrolledtext.ScrolledText(sec7, height=10, font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4", insertbackground="white")
        self.txt_log.grid(row=0, column=0, sticky=tk.NSEW)


        scroll_frame.columnconfigure(0, weight=1)
        scroll_frame.rowconfigure(row, weight=1)

        # 二开任务时隐藏 KB 文件路径行
        def _on_task_mode_change(*args):
            if self.task_mode.get() == "secondev":
                self._std_frame.grid_remove()
            else:
                self._std_frame.grid()
        if hasattr(self.task_mode, "trace_add"):
            self.task_mode.trace_add("write", _on_task_mode_change)
        else:
            self.task_mode.trace("w", _on_task_mode_change)
        self.parent.after_idle(_on_task_mode_change)


if __name__ == "__main__":
    root = tk.Tk()
    root.title("标准文件获取工具")
    root.geometry("820x780")
    root.minsize(640, 600)
    style = ttk.Style()
    style.theme_use("vista" if "vista" in style.theme_names() else "clam")
    frame = ttk.Frame(root, padding=8)
    frame.pack(fill=tk.BOTH, expand=True)
    app = SvnStandardFileTab(frame)
    app.build()
    root.mainloop()
