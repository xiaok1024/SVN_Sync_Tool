# -*- coding: utf-8 -*-
"""SVN 同步工具的 PySide6 主窗口。"""

import os
from dataclasses import dataclass

from PySide6.QtCore import QObject, QSignalBlocker, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QCloseEvent, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from qt_pages import PAGE_DEFINITIONS
from qt_theme import APP_STYLESHEET
from svn_sync_core import SVN_EXECUTABLE, SyncEngine


class LogBridge(QObject):
    append_requested = Signal(object, str)

    def __init__(self):
        super().__init__()
        self.append_requested.connect(self._append)

    @Slot(object, str)
    def _append(self, target, message):
        if target is None:
            return
        try:
            target.moveCursor(QTextCursor.End)
            target.insertPlainText(message)
            scrollbar = target.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
        except RuntimeError:
            pass


class QtSyncEngine(SyncEngine):
    def __init__(self, bridge):
        super().__init__()
        self._bridge = bridge

    def _log(self, target, message):
        if target is not None:
            self._bridge.append_requested.emit(target, message)


class AppContext(QObject):
    shared_changed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.thread_pool = QThreadPool.globalInstance()
        self.thread_pool.setMaxThreadCount(max(4, self.thread_pool.maxThreadCount()))
        self.log_bridge = LogBridge()
        self.engine = QtSyncEngine(self.log_bridge)
        self._shared = {
            "svn_url": "",
            "svn_user": "",
            "svn_pass": "",
            "smb_user": "",
            "smb_pass": "",
            "checkout_dir": "",
            "target_dir": "",
            "source_dir": "",
        }
        self._bindings = {}

    def bind_shared(self, key, field):
        self._bindings.setdefault(key, []).append(field)
        field.setText(self._shared.get(key, ""))
        field.textChanged.connect(lambda text, k=key, source=field: self.set_shared_text(k, text, source))

    def set_shared_text(self, key, text, source=None):
        value = str(text or "")
        self._shared[key] = value
        for field in self._bindings.get(key, []):
            if field is source or field.text() == value:
                continue
            blocker = QSignalBlocker(field)
            field.setText(value)
            del blocker
        self.shared_changed.emit(key, value)
        if key == "checkout_dir" and self._shared.get("target_dir") != value:
            self.set_shared_text("target_dir", value)

    def shared_text(self, key):
        return self._shared.get(key, "")

    @property
    def svn_user(self):
        return self.shared_text("svn_user").strip()

    @property
    def svn_pass(self):
        return self.shared_text("svn_pass")

    def apply_credentials(self):
        self.engine.svn_user = self.svn_user
        self.engine.svn_pass = self.svn_pass
        self.engine.smb_user = self.shared_text("smb_user").strip()
        self.engine.smb_pass = self.shared_text("smb_pass")


class CredentialsDialog(QDialog):
    def __init__(self, context, parent=None):
        super().__init__(parent)
        self.context = context
        self.setWindowTitle("连接凭据")
        self.setModal(True)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        title = QLabel("SVN 与网络共享凭据")
        title.setObjectName("cardTitle")
        hint = QLabel("密码只保留在本次运行内存中，不写入配置、日志或发布产物。")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(hint)

        self.svn_user = QLineEdit(context.shared_text("svn_user"))
        self.svn_user.setPlaceholderText("SVN 用户名（可选）")
        self.svn_pass = QLineEdit(context.shared_text("svn_pass"))
        self.svn_pass.setEchoMode(QLineEdit.Password)
        self.svn_pass.setPlaceholderText("SVN 密码；留空使用缓存")
        self.smb_user = QLineEdit(context.shared_text("smb_user"))
        self.smb_user.setPlaceholderText("SMB 用户名（macOS 挂载时可选）")
        self.smb_pass = QLineEdit(context.shared_text("smb_pass"))
        self.smb_pass.setEchoMode(QLineEdit.Password)
        self.smb_pass.setPlaceholderText("SMB 密码")
        for field in (self.svn_user, self.svn_pass, self.smb_user, self.smb_pass):
            layout.addWidget(field)

        reveal = QCheckBox("显示密码")
        reveal.toggled.connect(self._toggle_passwords)
        layout.addWidget(reveal)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存到本次会话")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _toggle_passwords(self, checked):
        mode = QLineEdit.Normal if checked else QLineEdit.Password
        self.svn_pass.setEchoMode(mode)
        self.smb_pass.setEchoMode(mode)

    def accept(self):
        self.context.set_shared_text("svn_user", self.svn_user.text())
        self.context.set_shared_text("svn_pass", self.svn_pass.text())
        self.context.set_shared_text("smb_user", self.smb_user.text())
        self.context.set_shared_text("smb_pass", self.smb_pass.text())
        self.context.apply_credentials()
        super().accept()


@dataclass(frozen=True)
class PageMeta:
    title: str
    subtitle: str


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.context = AppContext()
        self.setWindowTitle("SVN 代码同步工具")
        self.resize(1180, 790)
        self.setMinimumSize(980, 680)
        self.setStyleSheet(APP_STYLESHEET)
        self.page_meta = [
            PageMeta("SVN 拉取", "创建新的 SVN 工作副本"),
            PageMeta("交叉覆盖", "先扫描预览，再安全覆盖选中文件"),
            PageMeta("全自动流程", "拉取、覆盖与提交的可视化流程"),
            PageMeta("升级清单", "保留富文本颜色语义并生成 Markdown"),
            PageMeta("版本路径", "按版本查询并整理 SVN 文件 URL"),
            PageMeta("标准文件", "从 KB / 历史目录补全并提交标准版本"),
        ]
        self._build_ui()
        self._show_page(0)

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(214)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(14, 18, 14, 14)
        side_layout.setSpacing(12)
        brand = QHBoxLayout()
        mark = QLabel("SV")
        mark.setObjectName("brandMark")
        mark.setAlignment(Qt.AlignCenter)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(1)
        brand_title = QLabel("SVN Sync")
        brand_title.setObjectName("brandTitle")
        brand_sub = QLabel("升级辅助工作台")
        brand_sub.setObjectName("brandSub")
        brand_text.addWidget(brand_title)
        brand_text.addWidget(brand_sub)
        brand.addWidget(mark)
        brand.addLayout(brand_text, 1)
        side_layout.addLayout(brand)

        self.navigation = QListWidget()
        self.navigation.setObjectName("navList")
        self.navigation.setSpacing(2)
        nav_labels = ["01   SVN 拉取", "02   交叉覆盖", "03   全自动流程",
                      "04   升级清单", "05   版本路径", "06   标准文件"]
        for label in nav_labels:
            item = QListWidgetItem(label)
            item.setSizeHint(item.sizeHint().expandedTo(item.sizeHint()))
            self.navigation.addItem(item)
        self.navigation.currentRowChanged.connect(self._show_page)
        side_layout.addWidget(self.navigation, 1)
        hint = QLabel("Windows GUI · Qt 6\n所有业务共用核心引擎")
        hint.setObjectName("sidebarHint")
        hint.setWordWrap(True)
        side_layout.addWidget(hint)
        shell.addWidget(sidebar)

        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)
        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar.setFixedHeight(82)
        top_layout = QHBoxLayout(topbar)
        top_layout.setContentsMargins(26, 12, 22, 12)
        title_layout = QVBoxLayout()
        title_layout.setSpacing(2)
        self.page_title = QLabel()
        self.page_title.setObjectName("pageTitle")
        self.page_subtitle = QLabel()
        self.page_subtitle.setObjectName("pageSubtitle")
        title_layout.addWidget(self.page_title)
        title_layout.addWidget(self.page_subtitle)
        top_layout.addLayout(title_layout)
        top_layout.addStretch(1)
        self.status_dot = QLabel()
        self.status_dot.setObjectName("statusDot")
        self.app_status = QLabel("就绪")
        self.app_status.setObjectName("appStatus")
        credential_button = QPushButton("连接凭据")
        credential_button.clicked.connect(self._edit_credentials)
        top_layout.addWidget(self.status_dot)
        top_layout.addWidget(self.app_status)
        top_layout.addSpacing(12)
        top_layout.addWidget(credential_button)
        workspace_layout.addWidget(topbar)

        self.stack = QStackedWidget()
        self.pages = []
        for _short, _title, page_class in PAGE_DEFINITIONS:
            page = page_class(self.context)
            page.status_changed.connect(self.set_status)
            self.pages.append(page)
            self.stack.addWidget(page)
        workspace_layout.addWidget(self.stack, 1)
        shell.addWidget(workspace, 1)

        svn_name = os.path.basename(SVN_EXECUTABLE) if SVN_EXECUTABLE else "svn"
        self.statusBar().showMessage("SVN CLI: %s  ·  凭据仅保留于内存" % svn_name)

    def _show_page(self, index):
        if index < 0 or index >= len(self.page_meta):
            return
        self.stack.setCurrentIndex(index)
        if self.navigation.currentRow() != index:
            self.navigation.setCurrentRow(index)
        meta = self.page_meta[index]
        self.page_title.setText(meta.title)
        self.page_subtitle.setText(meta.subtitle)

    def set_status(self, message):
        self.app_status.setText(message)
        danger_words = ("失败", "错误", "异常", "终止")
        warning_words = ("取消", "警告", "准备", "正在")
        if any(word in message for word in danger_words):
            color = "#ef4444"
        elif any(word in message for word in warning_words):
            color = "#f59e0b"
        else:
            color = "#22c55e"
        self.status_dot.setStyleSheet("background: %s; border-radius: 4px;" % color)

    def _edit_credentials(self):
        dialog = CredentialsDialog(self.context, self)
        if dialog.exec() == QDialog.Accepted:
            self.set_status("连接凭据已更新（仅本次会话）")

    def closeEvent(self, event: QCloseEvent):
        self.context.engine._cleanup_temp_mounts()
        event.accept()
