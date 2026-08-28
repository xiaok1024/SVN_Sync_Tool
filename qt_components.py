# -*- coding: utf-8 -*-
"""Qt GUI 的通用组件。"""

import traceback

from PySide6.QtCore import QObject, QRunnable, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QListView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, function, *args, **kwargs):
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            value = self.function(*self.args, **self.kwargs)
        except Exception:
            self.signals.error.emit(traceback.format_exc())
        else:
            self.signals.result.emit(value)
        finally:
            self.signals.finished.emit()


class Card(QFrame):
    def __init__(self, title="", subtitle="", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(18, 16, 18, 18)
        self.layout.setSpacing(12)
        if title:
            title_label = QLabel(title)
            title_label.setObjectName("cardTitle")
            self.layout.addWidget(title_label)
        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setObjectName("cardSubtitle")
            subtitle_label.setWordWrap(True)
            self.layout.addWidget(subtitle_label)


class Page(QWidget):
    def __init__(self, parent=None, scroll=True):
        super().__init__(parent)
        self.setObjectName("page")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        if scroll:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.content = QWidget()
            self.content.setObjectName("page")
            self.content_layout = QVBoxLayout(self.content)
            self.content_layout.setContentsMargins(24, 22, 24, 24)
            self.content_layout.setSpacing(14)
            area.setWidget(self.content)
            root.addWidget(area)
        else:
            self.content = self
            self.content_layout = root
            self.content_layout.setContentsMargins(24, 22, 24, 24)
            self.content_layout.setSpacing(14)

    def finish(self):
        self.content_layout.addStretch(1)


class FieldRow(QWidget):
    def __init__(self, label, field, action=None, hint="", parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        self.setMinimumHeight(82 if hint else 60)
        title = QLabel(label)
        title.setObjectName("fieldLabel")
        outer.addWidget(title)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(field, 1)
        if action:
            row.addWidget(action)
        outer.addLayout(row)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("fieldHint")
            hint_label.setWordWrap(True)
            outer.addWidget(hint_label)


def primary_button(text, callback=None):
    button = QPushButton(text)
    button.setProperty("role", "primary")
    if callback:
        button.clicked.connect(callback)
    return button


def browse_button(callback):
    button = QPushButton("浏览…")
    button.clicked.connect(callback)
    return button


def password_edit():
    field = QLineEdit()
    field.setEchoMode(QLineEdit.Password)
    field.setPlaceholderText("留空使用系统缓存")
    return field


def set_button_busy(button, busy, normal_text, busy_text="处理中…"):
    button.setDisabled(busy)
    button.setText(busy_text if busy else normal_text)


def styled_combo(*items):
    """构造下拉框，并强制使用 Qt 自绘的弹出列表。

    macOS 上 QComboBox 默认弹出原生菜单，样式表里的 QAbstractItemView
    规则不会生效，弹出层观感与界面其余部分割裂；显式指定 QListView 后
    两个平台表现一致。``items`` 可以是文本，也可以是 ``(文本, 数据)``。
    """
    combo = QComboBox()
    combo.setView(QListView())
    for item in items:
        if isinstance(item, tuple):
            combo.addItem(*item)
        else:
            combo.addItem(item)
    return combo


def compact_row(*widgets, stretch=True):
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    for widget in widgets:
        layout.addWidget(widget)
    if stretch:
        layout.addStretch(1)
    return container


def make_separator():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setStyleSheet("color: #e8ecf2; background: #e8ecf2; max-height: 1px;")
    return line


def stretch_field(field):
    field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return field
