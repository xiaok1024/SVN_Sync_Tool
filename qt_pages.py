# -*- coding: utf-8 -*-
"""PySide6 / Qt Widgets 功能页面。"""

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import clipboard_core
import svn_path_generator as path_generator
import upgrade_list_core as upgrade_core
from qt_components import (
    Card,
    FieldRow,
    Page,
    Worker,
    browse_button,
    compact_row,
    password_edit,
    primary_button,
    set_button_busy,
)
from svn_standard_file_core import StandardFileService
from svn_sync_core import IS_MACOS, IS_WINDOWS
from svn_sync_workflow import resolve_and_scan_cross_files, run_auto_pipeline, run_checkout


def choose_directory(parent, field, title):
    value = QFileDialog.getExistingDirectory(parent, title, field.text().strip())
    if value:
        field.setText(value)


def copy_text(text, parent=None, message="已复制到剪贴板"):
    if not text.strip():
        QMessageBox.information(parent, "提示", "没有可复制的内容")
        return False
    QApplication.clipboard().setText(text)
    if parent and hasattr(parent, "set_status"):
        parent.set_status(message)
    return True


class BasePage(Page):
    status_changed = Signal(str)

    def __init__(self, context, scroll=True):
        super().__init__(scroll=scroll)
        self.context = context
        self.engine = context.engine
        self.pool = context.thread_pool
        self._workers = set()

    def set_status(self, message):
        self.status_changed.emit(message)

    def run_worker(self, function, on_result, on_error=None, on_finished=None):
        worker = Worker(function)
        self._workers.add(worker)
        worker.signals.result.connect(on_result)
        worker.signals.error.connect(on_error or self.show_worker_error)
        if on_finished:
            worker.signals.finished.connect(on_finished)
        worker.signals.finished.connect(lambda w=worker: self._workers.discard(w))
        self.pool.start(worker)
        return worker

    def show_worker_error(self, details):
        self.set_status("操作失败")
        QMessageBox.critical(self, "操作失败", details.splitlines()[-1] if details else "未知错误")


class CheckoutPage(BasePage):
    def __init__(self, context):
        super().__init__(context)
        intro = Card("拉取 SVN 工作副本", "输入仓库地址和本地目录，认证信息留空时使用系统 SVN 缓存。")
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://svn.example.com/svn/customer/ecology")
        intro.layout.addWidget(FieldRow("SVN 仓库地址", self.url))
        self.context.bind_shared("svn_url", self.url)

        auth = QWidget()
        auth_layout = QHBoxLayout(auth)
        auth_layout.setContentsMargins(0, 0, 0, 0)
        auth_layout.setSpacing(10)
        self.user = QLineEdit()
        self.user.setPlaceholderText("用户名（可选）")
        self.password = password_edit()
        auth_layout.addWidget(self.user)
        auth_layout.addWidget(self.password)
        intro.layout.addWidget(auth)
        self.context.bind_shared("svn_user", self.user)
        self.context.bind_shared("svn_pass", self.password)

        self.destination = QLineEdit()
        self.destination.setPlaceholderText("选择工作副本保存目录")
        browse = browse_button(lambda: choose_directory(self, self.destination, "选择 SVN 拉取目录"))
        intro.layout.addWidget(FieldRow("拉取到目录", self.destination, browse))
        self.context.bind_shared("checkout_dir", self.destination)
        self.run_button = primary_button("开始拉取", self.start_checkout)
        intro.layout.addWidget(compact_row(self.run_button))
        self.content_layout.addWidget(intro)

        logs = Card("执行日志", "SVN 命令中的密码会统一脱敏。")
        self.log = QPlainTextEdit()
        self.log.setObjectName("logView")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(300)
        logs.layout.addWidget(self.log)
        self.content_layout.addWidget(logs, 1)
        self.finish()

    def start_checkout(self):
        url = self.url.text().strip()
        destination = self.destination.text().strip()
        if not url or not destination:
            QMessageBox.warning(self, "参数不完整", "请填写 SVN 仓库地址和拉取目录")
            return
        self.log.clear()
        set_button_busy(self.run_button, True, "开始拉取", "正在拉取…")
        self.set_status("正在拉取 SVN 工作副本")
        self.context.apply_credentials()

        def task():
            return run_checkout(self.engine, url, destination, self.log)

        def done(result):
            ok, _output = result
            if ok:
                self.context.set_shared_text("checkout_dir", destination)
                self.context.set_shared_text("target_dir", destination)
                self.set_status("SVN 拉取完成")
            else:
                self.set_status("SVN 拉取失败")
                QMessageBox.warning(self, "拉取失败", "请查看执行日志中的 SVN 输出")

        self.run_worker(
            task, done,
            on_finished=lambda: set_button_busy(self.run_button, False, "开始拉取"))


class OverwritePage(BasePage):
    def __init__(self, context):
        super().__init__(context)
        config = Card("交叉文件覆盖", "按相同相对路径匹配来源文件；所有写入都必须先扫描预览并确认。")
        self.target = QLineEdit()
        self.source = QLineEdit()
        self.target.setPlaceholderText("SVN 工作副本（被覆盖）")
        self.source.setPlaceholderText("本地目录、UNC 或 smb:// 来源")
        config.layout.addWidget(FieldRow(
            "目标目录", self.target,
            browse_button(lambda: choose_directory(self, self.target, "选择 SVN 工作副本"))))
        config.layout.addWidget(FieldRow(
            "来源目录", self.source,
            browse_button(lambda: choose_directory(self, self.source, "选择整理好的来源目录")),
            "支持本地路径、UNC 与 smb://；macOS 可复用已挂载共享。"))
        self.context.bind_shared("target_dir", self.target)
        self.context.bind_shared("source_dir", self.source)

        self.smb_user = QLineEdit()
        self.smb_user.setPlaceholderText("SMB 账号（可选）")
        self.smb_pass = password_edit()
        self.context.bind_shared("smb_user", self.smb_user)
        self.context.bind_shared("smb_pass", self.smb_pass)
        config.layout.addWidget(compact_row(self.smb_user, self.smb_pass))
        self.scan_button = primary_button("扫描预览", self.start_scan)
        self.quick_button = QPushButton("一键覆盖")
        self.quick_button.clicked.connect(lambda: self.start_scan(quick=True))
        self.select_all_button = QPushButton("全选")
        self.select_all_button.clicked.connect(lambda: self.set_all_checked(True))
        self.clear_all_button = QPushButton("取消全选")
        self.clear_all_button.clicked.connect(lambda: self.set_all_checked(False))
        self.clear_button = QPushButton("清空结果")
        self.clear_button.clicked.connect(self.clear_results)
        self.cover_button = QPushButton("覆盖选中")
        self.cover_button.setProperty("role", "primary")
        self.cover_button.setEnabled(False)
        self.cover_button.clicked.connect(self.start_cover)
        config.layout.addWidget(compact_row(
            self.scan_button, self.quick_button, self.select_all_button, self.clear_all_button,
            self.cover_button, self.clear_button))
        self.content_layout.addWidget(config)

        result = Card("扫描结果", "勾选需要覆盖的文件。默认全选；点击覆盖前仍会再次确认。")
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["文件", "来源路径", "目标路径"])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setMinimumHeight(330)
        self.summary = QLabel("尚未扫描")
        self.summary.setObjectName("hint")
        result.layout.addWidget(self.table)
        result.layout.addWidget(self.summary)
        self.content_layout.addWidget(result, 1)
        self.entries = []

    def start_scan(self, _checked=False, quick=False):
        target, source = self.target.text().strip(), self.source.text().strip()
        if not target or not source:
            QMessageBox.warning(self, "参数不完整", "请填写目标目录和来源目录")
            return
        self.context.apply_credentials()
        set_button_busy(self.scan_button, True, "扫描预览", "扫描中…")
        set_button_busy(self.quick_button, True, "一键覆盖", "扫描中…")
        self.cover_button.setEnabled(False)
        self.table.setRowCount(0)
        self.set_status("正在扫描交叉文件")

        def task():
            return resolve_and_scan_cross_files(self.engine, target, source)

        def done(entries):
            self.entries = entries
            self.table.setRowCount(len(entries))
            for row, (relative, source_file, target_file) in enumerate(entries):
                item = QTableWidgetItem(relative)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked)
                self.table.setItem(row, 0, item)
                self.table.setItem(row, 1, QTableWidgetItem(source_file))
                self.table.setItem(row, 2, QTableWidgetItem(target_file))
            self.cover_button.setEnabled(bool(entries))
            self.summary.setText("找到 %d 个交叉文件，默认全部选中" % len(entries))
            self.set_status("扫描完成：%d 个交叉文件" % len(entries))
            if quick and entries:
                self.start_cover()

        self.run_worker(
            task, done,
            on_finished=self.finish_scan)

    def finish_scan(self):
        set_button_busy(self.scan_button, False, "扫描预览")
        set_button_busy(self.quick_button, False, "一键覆盖")

    def set_all_checked(self, checked):
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(state)

    def clear_results(self):
        self.entries = []
        self.table.setRowCount(0)
        self.summary.setText("尚未扫描")
        self.cover_button.setEnabled(False)
        self.set_status("交叉文件扫描结果已清空")

    def selected_entries(self):
        return [entry for row, entry in enumerate(self.entries)
                if self.table.item(row, 0).checkState() == Qt.Checked]

    def start_cover(self):
        entries = self.selected_entries()
        if not entries:
            QMessageBox.information(self, "提示", "请至少勾选一个文件")
            return
        answer = QMessageBox.question(
            self, "确认覆盖",
            "确定将 %d 个来源文件覆盖到目标工作副本？\n\n此操作会修改磁盘文件。" % len(entries),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        set_button_busy(self.cover_button, True, "覆盖选中", "覆盖中…")
        self.set_status("正在覆盖 %d 个文件" % len(entries))

        def done(result):
            copied, errors = result
            text = "覆盖完成：%d 成功，%d 失败" % (len(copied), len(errors))
            self.summary.setText(text)
            self.set_status(text)
            if errors:
                QMessageBox.warning(self, "部分文件失败", "\n".join(
                    "%s: %s" % item for item in errors[:12]))
            else:
                QMessageBox.information(self, "覆盖完成", text)

        self.run_worker(
            lambda: self.engine._copy_cross_files(entries), done,
            on_finished=lambda: set_button_busy(self.cover_button, False, "覆盖选中"))


class AutoPipelinePage(BasePage):
    progress_signal = Signal(int, str, str)

    def __init__(self, context):
        super().__init__(context)
        self.progress_signal.connect(self.update_progress)
        top = Card("全自动流程", "一次确认授权既定的拉取、交叉覆盖和提交；checkout 删除旧工作副本会单独确认。")
        self.url = QLineEdit()
        self.destination = QLineEdit()
        self.source = QLineEdit()
        self.message = QPlainTextEdit("自动同步代码")
        self.message.setMaximumHeight(72)
        top.layout.addWidget(FieldRow("SVN 仓库地址", self.url))
        top.layout.addWidget(FieldRow(
            "工作副本目录", self.destination,
            browse_button(lambda: choose_directory(self, self.destination, "选择工作副本目录"))))
        top.layout.addWidget(FieldRow(
            "整理好的来源目录", self.source,
            browse_button(lambda: choose_directory(self, self.source, "选择来源目录"))))
        top.layout.addWidget(FieldRow("SVN 提交信息", self.message))
        self.context.bind_shared("svn_url", self.url)
        self.context.bind_shared("checkout_dir", self.destination)
        self.context.bind_shared("source_dir", self.source)

        self.checkout_mode = QRadioButton("checkout（首次拉取）")
        self.update_mode = QRadioButton("update（已有则更新）")
        self.checkout_mode.setChecked(True)
        self.run_button = primary_button("执行完整流程", self.start_pipeline)
        top.layout.addWidget(compact_row(self.checkout_mode, self.update_mode, self.run_button))
        self.content_layout.addWidget(top)

        split = QSplitter(Qt.Horizontal)
        progress_card = Card("流程进度", "任一步骤失败都会立即终止，避免提交不完整变更。")
        self.step_labels = []
        for title in ("1  拉取工作副本", "2  覆盖交叉文件", "3  提交 SVN"):
            label = QLabel("○  " + title)
            label.setMinimumHeight(32)
            progress_card.layout.addWidget(label)
            self.step_labels.append(label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 3)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        progress_card.layout.addWidget(self.progress)
        progress_card.layout.addStretch(1)
        split.addWidget(progress_card)

        output_card = Card("执行日志")
        self.log = QPlainTextEdit()
        self.log.setObjectName("logView")
        self.log.setReadOnly(True)
        output_card.layout.addWidget(self.log)
        split.addWidget(output_card)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setMinimumHeight(310)
        self.content_layout.addWidget(split)

        paths = Card("提交文件路径", "提交成功或无新变更时，可复制对应版本的文件 URL。")
        self.path_output = QPlainTextEdit()
        self.path_output.setObjectName("pathView")
        self.path_output.setReadOnly(True)
        self.path_output.setMaximumHeight(110)
        self.copy_button = QPushButton("复制文件路径")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(
            lambda: copy_text(self.path_output.toPlainText(), self, "提交文件路径已复制"))
        paths.layout.addWidget(self.path_output)
        paths.layout.addWidget(compact_row(self.copy_button))
        self.content_layout.addWidget(paths)
        self.finish()

    def mode(self):
        return "update" if self.update_mode.isChecked() else "checkout"

    def start_pipeline(self):
        values = {
            "url": self.url.text().strip(),
            "destination": self.destination.text().strip(),
            "source": self.source.text().strip(),
            "message": self.message.toPlainText().strip(),
            "mode": self.mode(),
        }
        if not all(values.values()):
            QMessageBox.warning(self, "参数不完整", "请填写 SVN 地址、工作副本、来源目录和提交信息")
            return
        source_error = self.engine._precheck_source(values["source"])
        if source_error:
            QMessageBox.warning(self, "来源目录不可用", source_error)
            return
        summary = (
            "SVN 模式：{mode}\nSVN 地址：{url}\n工作副本：{destination}\n"
            "来源目录：{source}\n提交信息：{message}\n\n确认执行完整流程？"
        ).format(**values)
        if QMessageBox.question(
                self, "确认全自动流程", summary,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        checkout_exists = os.path.isdir(os.path.join(values["destination"], ".svn"))
        if checkout_exists and values["mode"] == "checkout":
            if QMessageBox.warning(
                    self, "确认删除已有工作副本",
                    "checkout 模式将删除并重新创建以下 SVN 工作副本：\n\n%s\n\n确定继续？"
                    % values["destination"],
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return

        self.context.apply_credentials()
        self.log.clear()
        self.path_output.clear()
        self.copy_button.setEnabled(False)
        self.progress.setValue(0)
        for index, title in enumerate(("拉取工作副本", "覆盖交叉文件", "提交 SVN"), 1):
            self.step_labels[index - 1].setText("○  %d  %s" % (index, title))
            self.step_labels[index - 1].setStyleSheet("")
        set_button_busy(self.run_button, True, "执行完整流程", "流程执行中…")
        self.set_status("全自动流程执行中")

        def progress(step, state, text):
            self.progress_signal.emit(step, state, text)

        def task():
            return run_auto_pipeline(
                self.engine, values["url"], values["destination"], values["source"],
                values["mode"], values["message"], self.log, progress)

        def done(result):
            self.set_status(result.message)
            if result.urls:
                self.path_output.setPlainText("\n".join(result.urls))
                self.copy_button.setEnabled(True)
            if result.ok:
                QMessageBox.information(self, "流程完成", result.message)
            else:
                QMessageBox.warning(self, "流程终止", result.message)

        self.run_worker(
            task, done,
            on_finished=lambda: set_button_busy(self.run_button, False, "执行完整流程"))

    def update_progress(self, step, state, text):
        icons = {"running": "◉", "done": "✓", "error": "!"}
        colors = {"running": "#4f7cff", "done": "#15803d", "error": "#b42318"}
        self.step_labels[step - 1].setText("%s  %d  %s" % (icons.get(state, "○"), step, text))
        self.step_labels[step - 1].setStyleSheet("color: %s; font-weight: 600;" % colors.get(state, "#344054"))
        if state == "done":
            self.progress.setValue(max(self.progress.value(), step))


class UpgradeListPage(BasePage):
    def __init__(self, context):
        super().__init__(context, scroll=False)
        actions = Card("升级清单提取", "从网页复制带颜色的富文本清单，保留红色迁移与黑色上下文语义。")
        self.extract_button = primary_button("从剪贴板提取", self.start_extract)
        clear_button = QPushButton("清空")
        clear_button.clicked.connect(self.clear)
        actions.layout.addWidget(compact_row(self.extract_button, clear_button))
        self.content_layout.addWidget(actions)

        split = QSplitter(Qt.Vertical)
        list_card = Card("提取清单", "可在生成前手工修正；格式为 QC 分组 + [red]/[black] SVN URL。")
        self.list_edit = QPlainTextEdit()
        self.list_edit.setPlaceholderText("提取结果会显示在这里…")
        list_card.layout.addWidget(self.list_edit)
        human_button = QPushButton("生成升级 Markdown")
        human_button.clicked.connect(lambda: self.generate("md"))
        ai_button = QPushButton("生成 AI Markdown")
        ai_button.clicked.connect(lambda: self.generate("ai"))
        copy_list_button = QPushButton("复制清单")
        copy_list_button.clicked.connect(
            lambda: copy_text(self.list_edit.toPlainText(), self, "清单已复制"))
        list_card.layout.addWidget(compact_row(copy_list_button, human_button, ai_button))
        split.addWidget(list_card)

        result_card = Card("生成结果")
        self.result_edit = QPlainTextEdit()
        self.result_edit.setObjectName("resultView")
        result_card.layout.addWidget(self.result_edit)
        save_button = QPushButton("另存为…")
        save_button.clicked.connect(self.save_result)
        copy_result_button = QPushButton("复制结果")
        copy_result_button.clicked.connect(
            lambda: copy_text(self.result_edit.toPlainText(), self, "生成结果已复制"))
        result_card.layout.addWidget(compact_row(copy_result_button, save_button))
        split.addWidget(result_card)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        self.content_layout.addWidget(split, 1)
        self.default_name = "upgrade-file-list.md"

    def start_extract(self):
        set_button_busy(self.extract_button, True, "从剪贴板提取", "正在读取…")
        self.set_status("正在读取富文本剪贴板")
        plain_clipboard = QApplication.clipboard().text()

        def task():
            html = ""
            kind = "html"
            if IS_WINDOWS:
                html = clipboard_core.read_clipboard_html_windows()
            elif IS_MACOS:
                html = clipboard_core.read_clipboard_html_macos()
            if not html.strip():
                html = plain_clipboard
                kind = "text"
            if not html.strip():
                raise RuntimeError("剪贴板没有内容，请先复制升级清单")
            lines = upgrade_core.rt_extract_list_from_html(html)
            file_lines = [line for line in lines if line.startswith(upgrade_core.RT_COLOR_PREFIXES)]
            if not file_lines:
                if kind == "text":
                    raise RuntimeError("剪贴板只有纯文本，无法获取红/黑颜色；请从浏览器复制富文本")
                raise RuntimeError("未识别到带版本号的 SVN 文件 URL")
            qc_count = sum(1 for line in lines if line.startswith("QC"))
            return "\n".join(lines), qc_count, len(file_lines)

        def done(result):
            text, qc_count, file_count = result
            self.list_edit.setPlainText(text)
            self.set_status("提取完成：%d 个 QC，%d 个文件行" % (qc_count, file_count))

        self.run_worker(
            task, done,
            on_finished=lambda: set_button_busy(self.extract_button, False, "从剪贴板提取"))

    def generate(self, output_format):
        source = self.list_edit.toPlainText().strip()
        if not source:
            QMessageBox.information(self, "提示", "请先提取或填写清单")
            return
        try:
            entries, customer, raw = upgrade_core.rt_parse_txt(source)
            if output_format == "md":
                result = upgrade_core.rt_build_human_md(entries)
                self.default_name = "upgrade-file-list.md"
                label = "升级 Markdown"
            else:
                result = upgrade_core.rt_build_ai_md(entries, customer, raw)
                self.default_name = "upgrade-file-list-ai.md"
                label = "AI Markdown"
        except Exception as exc:
            QMessageBox.warning(self, "生成失败", str(exc))
            return
        self.result_edit.setPlainText(result)
        self.set_status("已生成%s：%s，%d 个 QC" % (label, customer, len(entries)))

    def save_result(self):
        text = self.result_edit.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "提示", "没有可保存的结果")
            return
        path, _selected = QFileDialog.getSaveFileName(
            self, "保存 Markdown", self.default_name, "Markdown (*.md);;所有文件 (*.*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text if text.endswith("\n") else text + "\n")
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.set_status("已保存：" + path)

    def clear(self):
        self.list_edit.clear()
        self.result_edit.clear()
        self.set_status("升级清单页面已清空")


class RevisionPathsPage(BasePage):
    def __init__(self, context):
        super().__init__(context, scroll=False)
        config = Card("版本号路径生成", "查询一个或多个 SVN 版本，也可以直接粘贴已有路径进行本地排序。")
        self.url = QLineEdit()
        self.revisions = QLineEdit()
        self.revisions.setPlaceholderText("例如 123,456-460 1000")
        config.layout.addWidget(FieldRow("SVN 仓库地址", self.url))
        config.layout.addWidget(FieldRow(
            "版本号", self.revisions, hint="支持单版本、逗号/空格分隔和连续区间。"))
        self.context.bind_shared("svn_url", self.url)
        self.sort = QComboBox()
        self.sort.addItem("按版本排序", "rev")
        self.sort.addItem("按路径排序", "path")
        self.sort.addItem("按文件名排序", "name")
        self.generate_button = primary_button("查询并生成", self.generate)
        local_sort_button = QPushButton("仅排序下方内容")
        local_sort_button.clicked.connect(self.sort_local)
        copy_button = QPushButton("复制结果")
        copy_button.clicked.connect(
            lambda: copy_text(self.output.toPlainText(), self, "版本路径已复制"))
        clear_button = QPushButton("清空")
        clear_button.clicked.connect(self.clear)
        config.layout.addWidget(compact_row(
            self.sort, self.generate_button, local_sort_button, copy_button, clear_button))
        self.content_layout.addWidget(config)

        output = Card("文件路径", "查询错误会附在结果末尾；目录条目会自动过滤。")
        self.output = QPlainTextEdit()
        self.output.setObjectName("resultView")
        self.output.setPlaceholderText("也可以先在这里粘贴已有的 (Vxxx) 路径，再点击“仅排序下方内容”。")
        output.layout.addWidget(self.output)
        self.summary = QLabel("就绪")
        self.summary.setObjectName("hint")
        output.layout.addWidget(self.summary)
        self.content_layout.addWidget(output, 1)

    def current_sort(self):
        return self.sort.currentData()

    def generate(self):
        url, spec = self.url.text().strip(), self.revisions.text().strip()
        if not url or not spec:
            QMessageBox.warning(self, "参数不完整", "请填写 SVN 地址和版本号")
            return
        self.context.apply_credentials()
        user = self.context.svn_user
        password = self.context.svn_pass
        sort_key = self.current_sort()
        set_button_busy(self.generate_button, True, "查询并生成", "查询中…")
        self.set_status("正在查询 SVN 版本路径")

        def task():
            results, errors = path_generator.query_revision_paths(url, spec, user, password)
            rows = path_generator.build_revision_url_rows(results, url, sort_key)
            return rows, errors

        def done(result):
            rows, errors = result
            lines = [row[0] for row in rows]
            if errors:
                lines.extend(["", "--- 错误详情 ---", *errors[:10]])
            self.output.setPlainText("\n".join(lines))
            self.summary.setText("共 %d 个文件，%d 个错误" % (len(rows), len(errors)))
            self.set_status("版本路径生成完成：%d 个文件" % len(rows))

        self.run_worker(
            task, done,
            on_finished=lambda: set_button_busy(self.generate_button, False, "查询并生成"))

    def sort_local(self):
        lines = path_generator.sort_existing_urls(
            self.output.toPlainText().splitlines(), self.current_sort())
        if not lines:
            QMessageBox.information(self, "提示", "请先粘贴或生成路径")
            return
        self.output.setPlainText("\n".join(lines))
        self.summary.setText("已对 %d 条路径完成本地排序" % len(lines))
        self.set_status("本地路径排序完成")

    def clear(self):
        self.output.clear()
        self.summary.setText("就绪")


class StandardFilesPage(BasePage):
    def __init__(self, context):
        super().__init__(context)
        self.service = StandardFileService(self.engine)
        self.items = []
        self.covered_items = []
        self.commit_done = False
        self.cover_failed = False

        config = Card("标准文件获取", "升级任务优先 KB 后历史目录；二开任务仅从历史目录查找。")
        self.title = QLineEdit()
        self.title.setPlaceholderText("例如 QC123 标准文件补全")
        config.layout.addWidget(FieldRow("任务标题", self.title))
        self.upgrade_mode = QRadioButton("升级任务")
        self.secondev_mode = QRadioButton("二开任务")
        self.upgrade_mode.setChecked(True)
        self.upgrade_mode.toggled.connect(self.update_mode)
        config.layout.addWidget(compact_row(self.upgrade_mode, self.secondev_mode))

        self.svn_url = QLineEdit()
        self.target = QLineEdit()
        self.standard = QLineEdit()
        self.historical = QLineEdit()
        config.layout.addWidget(FieldRow("客户 SVN 地址", self.svn_url))
        config.layout.addWidget(FieldRow(
            "目标 SVN 工作副本", self.target,
            browse_button(lambda: choose_directory(self, self.target, "选择目标 SVN 工作副本"))))
        self.standard_row = FieldRow(
            "KB 文件路径", self.standard,
            browse_button(lambda: choose_directory(self, self.standard, "选择 KB 文件目录")))
        config.layout.addWidget(self.standard_row)
        config.layout.addWidget(FieldRow(
            "历史文件路径", self.historical,
            browse_button(lambda: choose_directory(self, self.historical, "选择历史文件目录")),
            "来源可使用本地、UNC 或 smb:// 地址。"))
        self.context.bind_shared("svn_url", self.svn_url)
        self.context.bind_shared("target_dir", self.target)
        self.content_layout.addWidget(config)

        files = Card("文件清单", "每行一个 SVN URL、ecology 相对路径，或包含 ecology 的本地绝对路径。")
        self.file_list = QPlainTextEdit()
        self.file_list.setMinimumHeight(135)
        paste = QPushButton("从剪贴板粘贴")
        paste.clicked.connect(lambda: self.file_list.setPlainText(QApplication.clipboard().text()))
        clear = QPushButton("清空")
        clear.clicked.connect(self.file_list.clear)
        files.layout.addWidget(compact_row(paste, clear))
        files.layout.addWidget(self.file_list)
        self.allow_existing = QCheckBox("允许覆盖已存在的文件")
        self.allow_existing.setChecked(True)
        self.auto_commit = QCheckBox("覆盖成功后自动进入提交准备")
        self.auto_commit.setChecked(True)
        files.layout.addWidget(compact_row(self.allow_existing, self.auto_commit))
        self.content_layout.addWidget(files)

        preview = Card("预览与执行", "提交前会完整展示目标工作副本的 svn status，并进行二次确认。")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["状态", "相对路径", "来源", "说明"])
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setMinimumHeight(210)
        preview.layout.addWidget(self.table)
        self.scan_button = primary_button("扫描预览", self.start_scan)
        self.cover_button = QPushButton("确认覆盖")
        self.cover_button.clicked.connect(self.start_cover)
        self.cover_button.setEnabled(False)
        self.commit_button = QPushButton("提交 SVN 标准文件")
        self.commit_button.clicked.connect(self.start_prepare_commit)
        self.commit_button.setEnabled(False)
        self.local_button = QPushButton("提交后覆盖本地")
        self.local_button.clicked.connect(self.start_local_cover)
        self.local_button.setEnabled(False)
        self.copy_button = QPushButton("复制提交文件路径")
        self.copy_button.clicked.connect(lambda: copy_text(self.commit_urls(), self, "提交路径已复制"))
        self.copy_button.setEnabled(False)
        preview.layout.addWidget(compact_row(
            self.scan_button, self.cover_button, self.commit_button,
            self.local_button, self.copy_button))
        self.content_layout.addWidget(preview)

        log_card = Card("执行日志")
        self.log = QPlainTextEdit()
        self.log.setObjectName("logView")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(220)
        clear_log_button = QPushButton("清空日志")
        clear_log_button.clicked.connect(self.log.clear)
        log_card.layout.addWidget(compact_row(clear_log_button))
        log_card.layout.addWidget(self.log)
        self.content_layout.addWidget(log_card)
        self.finish()
        self._commit_urls = []

    def mode(self):
        return "upgrade" if self.upgrade_mode.isChecked() else "secondev"

    def update_mode(self):
        self.standard_row.setVisible(self.upgrade_mode.isChecked())

    def lines(self):
        return [line.strip() for line in self.file_list.toPlainText().splitlines() if line.strip()]

    def validate_scan(self):
        errors = []
        if not self.title.text().strip():
            errors.append("任务标题不能为空")
        if not self.svn_url.text().strip():
            errors.append("客户 SVN 地址不能为空")
        if not os.path.isdir(self.target.text().strip()):
            errors.append("目标 SVN 工作副本不存在")
        if self.mode() == "upgrade" and not self.standard.text().strip():
            errors.append("升级任务需要填写 KB 文件路径")
        if not self.historical.text().strip():
            errors.append("历史文件路径不能为空")
        if not self.lines():
            errors.append("文件清单不能为空")
        return errors

    def start_scan(self):
        errors = self.validate_scan()
        if errors:
            QMessageBox.warning(self, "参数检查", "\n".join(errors))
            return
        self.context.apply_credentials()
        params = (
            self.lines(), self.svn_url.text().strip(), self.target.text().strip(),
            self.mode(), self.standard.text().strip(), self.historical.text().strip(),
            self.allow_existing.isChecked())
        self.items = []
        self.covered_items = []
        self.commit_done = False
        self.cover_failed = False
        self._commit_urls = []
        self.cover_button.setEnabled(False)
        self.commit_button.setEnabled(False)
        self.local_button.setEnabled(False)
        self.copy_button.setEnabled(False)
        self.log.clear()
        set_button_busy(self.scan_button, True, "扫描预览", "扫描中…")
        self.set_status("正在扫描标准文件来源")

        def task():
            return self.service.scan(*params)

        def done(result):
            self.items, parsed, details = result
            for line in details:
                self.log.appendPlainText(line)
            self.render_items()
            ready = sum(item.status == "待覆盖" for item in self.items)
            missing = sum(item.status == "未找到来源" for item in self.items)
            self.cover_button.setEnabled(ready > 0)
            text = "解析 %d 个路径：%d 个待覆盖，%d 个缺失" % (parsed, ready, missing)
            self.log.appendPlainText(text)
            self.set_status(text)

        self.run_worker(
            task, done,
            on_finished=lambda: set_button_busy(self.scan_button, False, "扫描预览"))

    def render_items(self):
        self.table.setRowCount(len(self.items))
        colors = {
            "待覆盖": QColor("#b45309"), "已覆盖": QColor("#15803d"),
            "已覆盖本地": QColor("#4f46e5"), "未找到来源": QColor("#b42318"),
            "内容相同": QColor("#667085"), "跳过(目标已存在)": QColor("#667085"),
        }
        for row, item in enumerate(self.items):
            status = QTableWidgetItem(item.status)
            status.setForeground(colors.get(item.status, QColor("#344054")))
            self.table.setItem(row, 0, status)
            self.table.setItem(row, 1, QTableWidgetItem(item.rel_path))
            source_text = item.source_label or "—"
            if item.local_source_file:
                source_text += " + 本地源"
            self.table.setItem(row, 2, QTableWidgetItem(source_text))
            self.table.setItem(row, 3, QTableWidgetItem(item.detail))

    def start_cover(self):
        ready = [item for item in self.items if item.status == "待覆盖"]
        if not ready:
            QMessageBox.information(self, "提示", "没有待覆盖的文件")
            return
        if QMessageBox.question(
                self, "确认覆盖", "确定覆盖 %d 个文件到目标 SVN 工作副本？" % len(ready),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        set_button_busy(self.cover_button, True, "确认覆盖", "覆盖中…")

        def done(result):
            covered, errors = result
            self.render_items()
            for item in covered:
                self.log.appendPlainText("ok %s <- %s" % (item.rel_path, item.source_label))
            for error in errors:
                self.log.appendPlainText("fail " + error)
            if errors:
                self.covered_items = []
                self.cover_failed = True
                self.commit_button.setEnabled(False)
                self.set_status("覆盖存在失败项，已阻止 SVN 提交")
                QMessageBox.warning(self, "覆盖未完成", "存在 %d 个失败项，未进入提交。" % len(errors))
                return
            self.covered_items = covered
            self.commit_button.setEnabled(bool(covered))
            self.set_status("标准文件覆盖完成：%d 个" % len(covered))
            if self.auto_commit.isChecked() and covered:
                self.start_prepare_commit()

        self.run_worker(
            lambda: self.service.cover(ready), done,
            on_finished=self.finish_standard_cover)

    def finish_standard_cover(self):
        self.cover_button.setText("确认覆盖")
        self.cover_button.setEnabled(
            not self.cover_failed and any(item.status == "待覆盖" for item in self.items))

    def commit_message(self):
        labels = {item.source_label for item in self.covered_items}
        if len(labels) > 1:
            source_label = "标准文件/历史文件"
        else:
            source_label = next(iter(labels), "标准文件")
        return "%s %s" % (self.title.text().strip(), source_label)

    def start_prepare_commit(self):
        if not self.covered_items:
            QMessageBox.information(self, "提示", "没有本次覆盖的文件可提交")
            return
        target = self.target.text().strip()
        set_button_busy(self.commit_button, True, "提交 SVN 标准文件", "准备提交…")
        self.set_status("正在准备 SVN 提交")

        def done(result):
            set_button_busy(self.commit_button, False, "提交 SVN 标准文件")
            ok, output, status = result
            if not ok:
                if output == "目标目录没有可提交的 SVN 变更":
                    self.commit_done = True
                    self.set_status("没有 SVN 内容变化，无需提交")
                    self.update_post_commit_buttons()
                else:
                    self.log.appendPlainText("提交准备失败: " + output)
                    self.set_status("SVN 提交准备失败")
                return
            if output.strip():
                self.log.appendPlainText("svn add 结果：\n" + output.rstrip())
            self.log.appendPlainText("\n即将提交整个目标工作副本，完整 svn status：\n" + status.rstrip())
            dialog = QMessageBox(self)
            dialog.setIcon(QMessageBox.Warning)
            dialog.setWindowTitle("确认 SVN 提交")
            dialog.setText("本次将提交整个目标 SVN 工作副本")
            dialog.setInformativeText(
                "未版本控制（?）文件不会自动加入，但其他已修改、已登记或删除的文件会一并提交。\n"
                "请展开详细信息核对完整 svn status。")
            dialog.setDetailedText(status)
            dialog.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            dialog.setDefaultButton(QMessageBox.No)
            if dialog.exec() != QMessageBox.Yes:
                self.log.appendPlainText("用户取消提交；文件覆盖和 svn add 状态已保留。")
                self.set_status("已取消 SVN 提交")
                return
            self.start_commit(target)

        def error(details):
            set_button_busy(self.commit_button, False, "提交 SVN 标准文件")
            self.show_worker_error(details)

        self.run_worker(
            lambda: self.service.prepare_commit(target, self.covered_items), done,
            on_error=error)

    def start_commit(self, target):
        set_button_busy(self.commit_button, True, "提交 SVN 标准文件", "提交中…")
        self.set_status("正在提交整个目标 SVN 工作副本")

        def done(result):
            ok, output, revision, urls, _rel_paths = result
            self.log.appendPlainText(output[:2000])
            if not ok:
                self.set_status("SVN 提交失败")
                QMessageBox.warning(self, "提交失败", "请查看执行日志")
                return
            self.commit_done = True
            self._commit_urls = urls
            if revision:
                self.log.appendPlainText("\n提交版本: r%d" % revision)
            for url in urls:
                self.log.appendPlainText("  " + url)
            self.set_status("SVN 提交成功" + ("：r%d" % revision if revision else ""))
            self.update_post_commit_buttons()
            QMessageBox.information(self, "提交成功", "SVN 提交成功" + ("，版本 r%d" % revision if revision else ""))

        self.run_worker(
            lambda: self.service.commit_working_copy(target, self.commit_message()), done,
            on_finished=self.finish_commit_button)

    def finish_commit_button(self):
        self.commit_button.setText("提交 SVN 标准文件")
        self.commit_button.setEnabled(not self.commit_done and bool(self.covered_items))

    def update_post_commit_buttons(self):
        local_ready = any(
            item.local_source_file and item.status == "已覆盖"
            for item in self.covered_items)
        self.local_button.setEnabled(self.commit_done and local_ready)
        self.copy_button.setEnabled(bool(self._commit_urls))
        self.commit_button.setEnabled(False)

    def start_local_cover(self):
        items = [item for item in self.covered_items
                 if item.local_source_file and item.status == "已覆盖"]
        if not self.commit_done or not items:
            QMessageBox.information(self, "提示", "没有可执行的提交后本地覆盖")
            return
        if QMessageBox.warning(
                self, "确认提交后覆盖本地",
                "将用 %d 个本地源文件覆盖刚提交的标准版本。\n"
                "这会重新产生本地修改，且不会再次提交。确认继续？" % len(items),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        set_button_busy(self.local_button, True, "提交后覆盖本地", "覆盖中…")

        def done(result):
            copied, errors = result
            self.render_items()
            for item in copied:
                self.log.appendPlainText("ok %s <- 本地源文件（未提交）" % item.rel_path)
            for error in errors:
                self.log.appendPlainText("fail " + error)
            self.set_status("本地覆盖完成：%d 成功，%d 失败" % (len(copied), len(errors)))
            self.local_button.setEnabled(False)

        target = self.target.text().strip()
        self.run_worker(
            lambda: self.service.overwrite_from_local_sources(target, items), done,
            on_finished=lambda: self._finish_local_cover_button())

    def _finish_local_cover_button(self):
        self.local_button.setText("提交后覆盖本地")
        self.local_button.setEnabled(False)

    def commit_urls(self):
        return "\n".join(self._commit_urls)


PAGE_DEFINITIONS = [
    ("拉取", "SVN 拉取", CheckoutPage),
    ("覆盖", "交叉覆盖", OverwritePage),
    ("自动", "全自动流程", AutoPipelinePage),
    ("清单", "升级清单", UpgradeListPage),
    ("路径", "版本路径", RevisionPathsPage),
    ("标准", "标准文件", StandardFilesPage),
]
