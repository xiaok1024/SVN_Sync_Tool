# -*- coding: utf-8 -*-
"""SVN 代码拉取 + 交叉文件覆盖 + 全自动提交"""

import os, sys, subprocess, threading, shutil
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
from pathlib import Path
try: import queue
except: import Queue as queue

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)


class SvnSyncTool:
    def __init__(self, root):
        self.root = root
        self.root.title("SVN 代码同步工具")
        self.root.geometry("920x760")
        self.root.minsize(760, 620)
        style = ttk.Style()
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        self.svn_url = tk.StringVar()
        self.svn_user = tk.StringVar()
        self.svn_pass = tk.StringVar()
        self.checkout_dir = tk.StringVar()
        self.source_dir = tk.StringVar()
        self.target_dir = tk.StringVar()
        self.mode_var = tk.StringVar(value="auto")
        self.log_queue = queue.Queue()
        self._build_ui()
        self._poll_log_queue()

    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))
        t1 = ttk.Frame(nb, padding=12)
        nb.add(t1, text="  1. SVN 拉取  ")
        self._build_tab1(t1)
        t2 = ttk.Frame(nb, padding=12)
        nb.add(t2, text="  2. 交叉覆盖  ")
        self._build_tab2(t2)
        t3 = ttk.Frame(nb, padding=12)
        nb.add(t3, text="  3. 全自动流程  ")
        self._build_tab3(t3)

    def _build_tab1(self, t1):
        row = 0
        ttk.Label(t1, text="SVN 仓库地址：", font=("Microsoft YaHei", 10)).grid(row=row, column=0, sticky=tk.W, pady=(0, 4)); row+=1
        ttk.Entry(t1, textvariable=self.svn_url, font=("Consolas", 10)).grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6)); row+=1
        af = ttk.Frame(t1)
        af.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        ttk.Label(af, text="用户名：", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        ttk.Entry(af, textvariable=self.svn_user, font=("Consolas", 9), width=18).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(af, text="密码：", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        ttk.Entry(af, textvariable=self.svn_pass, font=("Consolas", 9), width=18, show="*").pack(side=tk.LEFT, padx=(0, 0))
        ttk.Label(af, text="（留空使用缓存）", font=("Microsoft YaHei", 8)).pack(side=tk.LEFT, padx=(6, 0))
        row += 1
        ttk.Label(t1, text="拉取到目录：", font=("Microsoft YaHei", 10)).grid(row=row, column=0, sticky=tk.W, pady=(0, 4)); row+=1
        frm = ttk.Frame(t1)
        frm.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        ttk.Entry(frm, textvariable=self.checkout_dir, font=("Consolas", 10)).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(frm, text="浏览...", command=self._browse_checkout).pack(side=tk.RIGHT, padx=(6, 0))
        row+=1
        self.btn_co = ttk.Button(t1, text="拉取代码", command=self._start_checkout)
        self.btn_co.grid(row=row, column=0, columnspan=3, pady=(0, 10)); row+=1
        ttk.Label(t1, text="执行日志：", font=("Microsoft YaHei", 9)).grid(row=row, column=0, sticky=tk.W); row+=1
        self.log_co = scrolledtext.ScrolledText(t1, height=14, font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4", insertbackground="white")
        self.log_co.grid(row=row, column=0, columnspan=3, sticky=tk.NSEW); row+=1
        t1.columnconfigure(1, weight=1)
        t1.rowconfigure(row, weight=1)

    def _build_tab2(self, t2):
        row = 0
        ttk.Label(t2, text="SVN 拉取目录（目标，被覆盖）：", font=("Microsoft YaHei", 10)).grid(row=row, column=0, sticky=tk.W, pady=(0, 4)); row+=1
        f2 = ttk.Frame(t2)
        f2.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        ttk.Entry(f2, textvariable=self.target_dir, font=("Consolas", 10)).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(f2, text="浏览...", command=self._browse_target).pack(side=tk.RIGHT, padx=(6, 0))
        row+=1
        ttk.Label(t2, text="整理好的目录（来源，取文件）：", font=("Microsoft YaHei", 10)).grid(row=row, column=0, sticky=tk.W, pady=(0, 4)); row+=1
        f1 = ttk.Frame(t2)
        f1.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        ttk.Entry(f1, textvariable=self.source_dir, font=("Consolas", 10)).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(f1, text="浏览...", command=self._browse_source).pack(side=tk.RIGHT, padx=(6, 0))
        row+=1
        bf = ttk.Frame(t2)
        bf.grid(row=row, column=0, columnspan=3, pady=(0, 10))
        self.btn_scan = ttk.Button(bf, text="扫描预览", command=self._start_scan)
        self.btn_scan.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_quick = ttk.Button(bf, text="一键覆盖（推荐）", command=self._start_quick_overwrite)
        self.btn_quick.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_ow = ttk.Button(bf, text="覆盖选中", command=self._start_overwrite, state=tk.DISABLED)
        self.btn_ow.pack(side=tk.LEFT)
        ttk.Button(bf, text="清空结果", command=self._clear_results).pack(side=tk.LEFT, padx=(8, 0))
        row+=1
        ttk.Label(t2, text="文件列表（点击切换勾选）：", font=("Microsoft YaHei", 9)).grid(row=row, column=0, sticky=tk.W); row+=1
        lf = ttk.Frame(t2)
        lf.grid(row=row, column=0, columnspan=3, sticky=tk.NSEW); row+=1
        self.tree = ttk.Treeview(lf, columns=("src", "tgt"), show="tree headings", height=8)
        self.tree.heading("#0", text="文件名")
        self.tree.heading("src", text="来源路径")
        self.tree.heading("tgt", text="目标路径")
        self.tree.column("#0", width=260, minwidth=180)
        self.tree.column("src", width=240, minwidth=160)
        self.tree.column("tgt", width=240, minwidth=160)
        vsb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.lbl_st = ttk.Label(t2, text="就绪", font=("Microsoft YaHei", 9))
        self.lbl_st.grid(row=row, column=0, sticky=tk.W, pady=(4, 0))
        self.lbl_cnt = ttk.Label(t2, text="共 0 个文件", font=("Microsoft YaHei", 9))
        self.lbl_cnt.grid(row=row, column=2, sticky=tk.E, pady=(4, 0))
        t2.columnconfigure(1, weight=1)
        t2.rowconfigure(row, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._on_click)
        self._xf = []
        self._ck = set()

    def _build_tab3(self, t3):
        row = 0
        af = ttk.Frame(t3)
        af.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8))
        ttk.Label(af, text="用户名：", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        ttk.Entry(af, textvariable=self.svn_user, font=("Consolas", 9), width=16).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(af, text="密码：", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        ttk.Entry(af, textvariable=self.svn_pass, font=("Consolas", 9), width=16, show="*").pack(side=tk.LEFT, padx=(0, 0))
        ttk.Label(af, text="（留空使用缓存）", font=("Microsoft YaHei", 8)).pack(side=tk.LEFT, padx=(6, 0))
        row += 1
        ttk.Label(t3, text="SVN 仓库地址：", font=("Microsoft YaHei", 10)).grid(row=row, column=0, sticky=tk.W, pady=(0, 2)); row+=1
        ttk.Entry(t3, textvariable=self.svn_url, font=("Consolas", 10)).grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6)); row+=1
        ttk.Label(t3, text="SVN 拉取目录：", font=("Microsoft YaHei", 10)).grid(row=row, column=0, sticky=tk.W, pady=(0, 2)); row+=1
        f_a = ttk.Frame(t3)
        f_a.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 6))
        ttk.Entry(f_a, textvariable=self.checkout_dir, font=("Consolas", 10)).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(f_a, text="浏览...", command=self._browse_checkout).pack(side=tk.RIGHT, padx=(6, 0))
        row+=1
        ttk.Label(t3, text="整理好的目录（来源取文件）：", font=("Microsoft YaHei", 10)).grid(row=row, column=0, sticky=tk.W, pady=(0, 2)); row+=1
        f_b = ttk.Frame(t3)
        f_b.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 10))
        ttk.Entry(f_b, textvariable=self.source_dir, font=("Consolas", 10)).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(f_b, text="浏览...", command=self._browse_source).pack(side=tk.RIGHT, padx=(6, 0))
        row+=1
        mode_f = ttk.Frame(t3)
        mode_f.grid(row=row, column=0, columnspan=3, sticky=tk.W, pady=(0, 6))
        ttk.Label(mode_f, text="拉取模式：", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        ttk.Radiobutton(mode_f, text="checkout（首次拉取）", variable=self.mode_var, value="checkout").pack(side=tk.LEFT, padx=(6, 12))
        ttk.Radiobutton(mode_f, text="update（已有则更新）", variable=self.mode_var, value="update").pack(side=tk.LEFT, padx=(0, 6))
        row += 1
        ttk.Label(t3, text="SVN 提交信息：", font=("Microsoft YaHei", 10)).grid(row=row, column=0, sticky=tk.W, pady=(0, 2)); row+=1
        self.auto_msg = scrolledtext.ScrolledText(t3, height=4, font=("Microsoft YaHei", 10), wrap=tk.WORD)
        self.auto_msg.grid(row=row, column=0, columnspan=3, sticky=tk.EW, pady=(0, 8))
        self.auto_msg.insert(tk.END, "自动同步代码")
        row += 1
        self.btn_auto = ttk.Button(t3, text="▶ 一键执行：拉取 -> 覆盖 -> 提交", command=self._start_auto_pipeline)
        self.btn_auto.grid(row=row, column=0, columnspan=3, pady=(0, 10), ipady=4)
        row += 1
        ttk.Label(t3, text="执行日志：", font=("Microsoft YaHei", 9)).grid(row=row, column=0, sticky=tk.W); row+=1
        self.log_auto = scrolledtext.ScrolledText(t3, height=12, font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4", insertbackground="white")
        self.log_auto.grid(row=row, column=0, columnspan=3, sticky=tk.NSEW); row+=1
        t3.columnconfigure(1, weight=1)
        t3.rowconfigure(row, weight=1)

    def _browse_checkout(self):
        d = filedialog.askdirectory(title="选择 SVN 拉取目标目录")
        if d: self.checkout_dir.set(d)
    def _browse_source(self):
        d = filedialog.askdirectory(title="选择整理好的目录（来源）")
        if d: self.source_dir.set(d)
    def _browse_target(self):
        d = filedialog.askdirectory(title="选择 SVN 拉取目录（目标）")
        if d: self.target_dir.set(d)

    def _log(self, w, m):
        self.log_queue.put((w, m))

    def _poll_log_queue(self):
        try:
            while True:
                w, m = self.log_queue.get_nowait()
                w.insert(tk.END, m); w.see(tk.END)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    def _build_svn_cmd(self, *args):
        cmd = ["svn", "--non-interactive",
               "--trust-server-cert-failures=unknown-ca,cn-mismatch,expired,not-yet-valid,other"]
        u = self.svn_user.get().strip()
        p = self.svn_pass.get().strip()
        if u:
            cmd.extend(["--username", u])
            if p:
                cmd.extend(["--password", p])
            else:
                cmd.append("--no-auth-cache")
        cmd.extend(args)
        return cmd

    def _run_svn(self, log_widget, *args):
        cmd = self._build_svn_cmd(*args)
        self._log(log_widget, ">> " + " ".join(cmd) + "\n")
        proc = subprocess.Popen(cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW)
        out_lines = []
        for line in proc.stdout:
            self._log(log_widget, line)
            out_lines.append(line)
        proc.wait()
        return proc.returncode, "".join(out_lines)

    def _start_checkout(self):
        url = self.svn_url.get().strip()
        if not url: messagebox.showwarning("提示", "请先输入 SVN 仓库地址"); return
        dst = self.checkout_dir.get().strip()
        if not dst: messagebox.showwarning("提示", "请选择拉取目录"); return
        self.btn_co.config(state=tk.DISABLED, text="正在拉取...")
        self.log_co.delete(1.0, tk.END)

        def run():
            try:
                rc, _ = self._run_svn(self.log_co, "checkout", url, dst)
                if rc == 0:
                    self._log(self.log_co, "\n--- 拉取完成 ---\n")
                    self.target_dir.set(dst)
                else:
                    self._log(self.log_co, "\n--- 拉取失败，返回码: " + str(rc) + " ---\n")
            except Exception as e:
                self._log(self.log_co, "\n--- 错误: " + str(e) + " ---\n")
            finally:
                self.root.after(0, lambda: self.btn_co.config(state=tk.NORMAL, text="拉取代码"))

        threading.Thread(target=run, daemon=True).start()

    def _scan_cross_files(self, tgt, src):
        results = []
        tp = Path(tgt)
        sp = Path(src)
        for f in sorted(tp.rglob("*")):
            if not f.is_file(): continue
            if ".svn" in f.parts or ".git" in f.parts: continue
            rel = f.relative_to(tp)
            src_file = sp / rel
            if src_file.exists() and src_file.is_file():
                results.append((str(rel), str(src_file), str(f)))
        return results

    def _start_scan(self):
        src = self.source_dir.get().strip()
        tgt = self.target_dir.get().strip()
        if not src or not tgt: messagebox.showwarning("提示", "请先选择来源目录和目标目录"); return
        if not os.path.isdir(src): messagebox.showerror("错误", "来源目录不存在: " + src); return
        if not os.path.isdir(tgt): messagebox.showerror("错误", "目标目录不存在: " + tgt); return
        self._clear_results()
        self.btn_scan.config(state=tk.DISABLED, text="扫描中...")
        self.lbl_st.config(text="正在扫描...")

        def run():
            res = self._scan_cross_files(tgt, src)
            self.root.after(0, lambda: self._show_scan(res))
        threading.Thread(target=run, daemon=True).start()

    def _show_scan(self, res):
        self._xf = res; self._ck.clear()
        self.tree.delete(*self.tree.get_children())
        if not res:
            self.lbl_st.config(text="未发现交叉文件"); self.lbl_cnt.config(text="共 0 个文件")
            self.btn_ow.config(state=tk.DISABLED); self.btn_scan.config(state=tk.NORMAL, text="扫描预览")
            return
        for i, (r, sa, ta) in enumerate(res):
            iid = "i" + str(i)
            self.tree.insert("", tk.END, iid=iid, text=r, values=(sa, ta))
            self._ck.add(iid)
        self.lbl_st.config(text="扫描完成 - 点击条目切换勾选")
        self.lbl_cnt.config(text="共 " + str(len(res)) + " 个文件 (全部默认勾选)")
        self.btn_ow.config(state=tk.NORMAL)
        self.btn_scan.config(state=tk.NORMAL, text="扫描预览")

    def _on_click(self, evt):
        for iid in self.tree.selection():
            if iid in self._ck: self._ck.discard(iid)
            else: self._ck.add(iid)
        self.tree.selection_remove(*self.tree.selection())
        self.lbl_cnt.config(text="已选 " + str(len(self._ck)) + " / " + str(len(self._xf)) + " 个")

    def _start_quick_overwrite(self):
        src = self.source_dir.get().strip()
        tgt = self.target_dir.get().strip()
        if not src or not tgt: messagebox.showwarning("提示", "请先选择来源目录和目标目录"); return
        if not os.path.isdir(src): messagebox.showerror("错误", "来源目录不存在: " + src); return
        if not os.path.isdir(tgt): messagebox.showerror("错误", "目标目录不存在: " + tgt); return
        self._clear_results()
        self.btn_quick.config(state=tk.DISABLED, text="正在覆盖...")
        self.lbl_st.config(text="正在扫描并覆盖...")

        def run():
            try:
                res = self._scan_cross_files(tgt, src)
                if not res:
                    self.root.after(0, lambda: self._quick_done(0, 0, "未找到匹配的交叉文件"))
                    return
                ok = 0; fail = 0
                for rel, sa, ta in res:
                    try:
                        Path(ta).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(sa, ta); ok += 1
                    except: fail += 1
                self.root.after(0, lambda: self._quick_done(ok, fail, ""))
            except Exception as e:
                self.root.after(0, lambda: self._quick_done(0, 0, "错误: " + str(e)))
        threading.Thread(target=run, daemon=True).start()

    def _quick_done(self, ok, fail, err):
        if err:
            self.lbl_st.config(text=err); self.lbl_cnt.config(text="")
        else:
            msg = "覆盖完成! 成功 " + str(ok) + " 个"
            if fail: msg += ", 失败 " + str(fail) + " 个"
            self.lbl_st.config(text=msg); self.lbl_cnt.config(text="")
        self.btn_quick.config(state=tk.NORMAL, text="一键覆盖（推荐）")

    def _start_overwrite(self):
        if not self._ck: messagebox.showinfo("提示", "请勾选要覆盖的文件"); return
        if not messagebox.askyesno("确认", "确定覆盖 " + str(len(self._ck)) + " 个文件？\n此操作不可撤销！"): return
        self.btn_ow.config(state=tk.DISABLED, text="覆盖中...")
        self.lbl_st.config(text="正在覆盖...")

        def run():
            ok = 0; fail = 0
            for i, (r, sa, ta) in enumerate(self._xf):
                if ("i" + str(i)) not in self._ck: continue
                try:
                    Path(ta).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(sa, ta); ok += 1
                except: fail += 1
            self.root.after(0, lambda: self._done(ok, fail))
        threading.Thread(target=run, daemon=True).start()

    def _done(self, ok, fail):
        msg = "覆盖完成! 成功 " + str(ok) + " 个"
        if fail: msg += ", 失败 " + str(fail) + " 个"
        self.lbl_st.config(text=msg); self.lbl_cnt.config(text="")
        self.btn_ow.config(state=tk.NORMAL, text="覆盖选中")
        messagebox.showinfo("完成", msg)

    def _clear_results(self):
        self._xf = []; self._ck.clear()
        self.tree.delete(*self.tree.get_children())
        self.lbl_st.config(text="就绪"); self.lbl_cnt.config(text="共 0 个文件")
        self.btn_ow.config(state=tk.DISABLED)

    def _start_auto_pipeline(self):
        url = self.svn_url.get().strip()
        dst = self.checkout_dir.get().strip()
        src = self.source_dir.get().strip()
        mode = self.mode_var.get()

        if not url: messagebox.showwarning("提示", "请先输入 SVN 仓库地址"); return
        if not dst: messagebox.showwarning("提示", "请选择 SVN 拉取目录"); return
        if not src: messagebox.showwarning("提示", "请选择整理好的目录"); return
        if not os.path.isdir(src): messagebox.showerror("错误", "来源目录不存在: " + src); return

        msg = self.auto_msg.get(1.0, tk.END).strip()
        if not msg: messagebox.showwarning("提示", "请输入提交信息"); return

        self.btn_auto.config(state=tk.DISABLED, text="⏳ 正在执行...")
        self.log_auto.delete(1.0, tk.END)

        def run():
            log = self.log_auto
            overall_ok = True
            try:
                # Step 1
                self._log(log, "【步骤 1/3】SVN 拉取\n")
                self._log(log, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
                dst_exists = os.path.isdir(dst) and os.path.isdir(os.path.join(dst, ".svn"))
                if dst_exists and mode == "update":
                    self._log(log, "目录已存在，执行 svn update...\n")
                    rc, _ = self._run_svn(log, "update", dst)
                else:
                    if dst_exists:
                        self._log(log, "目录已存在但选择 checkout 模式，先删除再拉取...\n")
                        shutil.rmtree(dst, ignore_errors=True)
                    rc, _ = self._run_svn(log, "checkout", url, dst)

                if rc != 0:
                    self._log(log, "\n❌ 步骤 1 失败，终止流程\n")
                    self.root.after(0, lambda: self._auto_done(False))
                    return
                self.target_dir.set(dst)
                self._log(log, "\n✅ 步骤 1 完成\n\n")

                # Step 2
                self._log(log, "【步骤 2/3】交叉文件覆盖\n")
                self._log(log, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
                res = self._scan_cross_files(dst, src)
                if not res:
                    self._log(log, "未找到交叉文件，跳过覆盖\n\n")
                else:
                    ok = 0; fail = 0
                    for rel, sa, ta in res:
                        try:
                            Path(ta).parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(sa, ta)
                            self._log(log, "  [覆盖] " + rel + "\n")
                            ok += 1
                        except Exception as e:
                            self._log(log, "  [失败] " + rel + " - " + str(e) + "\n")
                            fail += 1
                    self._log(log, "覆盖结果: 成功 " + str(ok) + " 个")
                    if fail: self._log(log, ", 失败 " + str(fail) + " 个")
                    self._log(log, "\n\n✅ 步骤 2 完成\n\n")

                # Step 3
                self._log(log, "【步骤 3/3】SVN 提交\n")
                self._log(log, "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
                self._log(log, "检查变更状态...\n")
                rc, status_out = self._run_svn(log, "status", dst)
                changed = [l for l in status_out.split("\n") if l.strip() and ".svn" not in l]
                if not changed:
                    self._log(log, "无变更需要提交\n")
                else:
                    self._log(log, "共 " + str(len(changed)) + " 个文件有变更\n")
                    rc, _ = self._run_svn(log, "commit", dst, "-m", msg)
                    if rc == 0:
                        self._log(log, "\n✅ 提交成功！\n")
                    else:
                        self._log(log, "\n❌ 提交失败，返回码: " + str(rc) + "\n")
                        overall_ok = False

                self._log(log, "\n" + "=" * 45 + "\n")
                self._log(log, "全自动流程结束\n")
            except Exception as e:
                self._log(log, "\n❌ 流程异常: " + str(e) + "\n")
                overall_ok = False
            finally:
                self.root.after(0, lambda: self._auto_done(overall_ok))

        threading.Thread(target=run, daemon=True).start()

    def _auto_done(self, ok):
        self.btn_auto.config(state=tk.NORMAL, text="▶ 一键执行：拉取 -> 覆盖 -> 提交")
        if ok:
            messagebox.showinfo("完成", "全自动流程执行完成！")

    def sync_checkout_to_target(self, *args):
        self.target_dir.set(self.checkout_dir.get())


if __name__ == "__main__":
    root = tk.Tk()
    app = SvnSyncTool(root)
    app.checkout_dir.trace("w", app.sync_checkout_to_target)
    root.mainloop()
