# -*- coding: utf-8 -*-
"""SVN 同步工具的 Qt 视觉令牌与样式表。"""

import sys
from pathlib import Path


def _asset_url(name):
    """返回 qt_assets 下资源的绝对路径。

    样式表里的 ``url()`` 按进程工作目录解析相对路径并不可靠，因此统一取绝对
    路径；PyInstaller 单文件模式会把 datas 解包到 ``sys._MEIPASS``。
    """
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parent
    return (root / "qt_assets" / name).as_posix()


_STYLESHEET_TEMPLATE = r"""
* {
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif;
    font-size: 13px;
    color: #172033;
}
QMainWindow, QWidget#appRoot { background: #f4f7fb; }
QFrame#sidebar {
    background: #111a2e;
    border: none;
}
QLabel#brandMark {
    min-width: 38px; max-width: 38px;
    min-height: 38px; max-height: 38px;
    border-radius: 11px;
    background: #4f7cff;
    color: white;
    font-size: 18px;
    font-weight: 700;
}
QLabel#brandTitle { color: #ffffff; font-size: 16px; font-weight: 700; }
QLabel#brandSub { color: #8e9bb5; font-size: 11px; }
QListWidget#navList {
    background: transparent;
    border: none;
    outline: none;
    color: #aeb8cb;
    padding: 4px 8px;
}
QListWidget#navList::item {
    border-radius: 9px;
    padding: 11px 12px;
    margin: 2px 0;
    color: #aeb8cb;
}
QListWidget#navList::item:hover { background: #1a2742; color: #ffffff; }
QListWidget#navList::item:selected { background: #25375d; color: #ffffff; }
QLabel#sidebarHint { color: #72809b; font-size: 11px; padding: 8px 14px; }
QFrame#topbar {
    background: #ffffff;
    border-bottom: 1px solid #e5eaf2;
}
QLabel#pageTitle { font-size: 22px; font-weight: 700; color: #111827; }
QLabel#pageSubtitle { font-size: 12px; color: #6b768a; }
QLabel#statusDot {
    min-width: 8px; max-width: 8px;
    min-height: 8px; max-height: 8px;
    border-radius: 4px;
    background: #22c55e;
}
QLabel#appStatus { color: #667085; font-size: 12px; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QFrame#page, QWidget#page { background: transparent; }
QFrame#card {
    background: #ffffff;
    border: 1px solid #e3e8f0;
    border-radius: 13px;
}
QLabel#cardTitle { color: #172033; font-size: 14px; font-weight: 700; }
QLabel#cardSubtitle, QLabel#hint, QLabel#fieldHint { color: #7a8496; font-size: 11px; }
QLabel#sectionKicker { color: #4f7cff; font-size: 11px; font-weight: 700; }
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox {
    background: #ffffff;
    border: 1px solid #d9e0ea;
    border-radius: 8px;
    padding: 7px 9px;
    selection-background-color: #cddaff;
}
QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover { border-color: #aebbd0; }
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border: 1px solid #4f7cff;
}
QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled {
    background: #f3f5f8;
    color: #9aa3b2;
}
/* 下拉框与按钮同排显示，几何和配色对齐 QPushButton，不再沿用输入框规则；
   同时接管 drop-down 区域，避免 macOS 原生控件外观混进扁平卡片界面。 */
QComboBox {
    background: #ffffff;
    border: 1px solid #d7dee9;
    border-radius: 8px;
    padding: 7px 9px;
    padding-right: 26px;
    min-height: 18px;
    color: #344054;
    selection-background-color: #cddaff;
}
QComboBox:hover { background: #f7f9fc; border-color: #aebbd0; }
QComboBox:focus { border-color: #4f7cff; }
QComboBox:disabled { color: #a9b1bf; background: #f3f5f8; border-color: #e5e8ee; }
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 22px;
    border: none;
    background: transparent;
}
QComboBox::down-arrow {
    width: 12px;
    height: 12px;
    image: url("__CHEVRON__");
}
QComboBox::down-arrow:disabled { image: url("__CHEVRON_DISABLED__"); }
/* 弹出列表：需配合 qt_components.styled_combo 显式指定 QListView，
   否则 macOS 会弹出原生菜单，下面这些规则不会生效。 */
QComboBox QAbstractItemView {
    border: 1px solid #d7dee9;
    background: #ffffff;
    padding: 4px;
    outline: none;
}
QComboBox QAbstractItemView::item {
    min-height: 26px;
    padding: 0 8px;
    border-radius: 6px;
    color: #344054;
}
QComboBox QAbstractItemView::item:hover { background: #f2f5fb; }
QComboBox QAbstractItemView::item:selected { background: #eef2ff; color: #1d3b8f; }
QPlainTextEdit#logView, QPlainTextEdit#resultView {
    background: #111827;
    color: #d8e0ee;
    border: 1px solid #1f2937;
    font-family: "Cascadia Mono", "Consolas", "Menlo", monospace;
    font-size: 12px;
    padding: 10px;
}
QPlainTextEdit#pathView { color: #a7f3d0; }
QPushButton {
    background: #ffffff;
    border: 1px solid #d7dee9;
    border-radius: 8px;
    padding: 7px 13px;
    min-height: 18px;
    color: #344054;
}
QPushButton:hover { background: #f7f9fc; border-color: #aebbd0; }
QPushButton:pressed { background: #eef2f8; }
QPushButton:disabled { color: #a9b1bf; background: #f3f5f8; border-color: #e5e8ee; }
QPushButton[role="primary"] {
    background: #4f7cff;
    color: white;
    border-color: #4f7cff;
    font-weight: 600;
}
QPushButton[role="primary"]:hover { background: #426eea; border-color: #426eea; }
QPushButton[role="danger"] { color: #b42318; border-color: #f0b7b3; background: #fff8f7; }
QToolButton {
    background: transparent;
    border: none;
    border-radius: 7px;
    padding: 6px 9px;
    color: #536176;
}
QToolButton:hover { background: #eef2f7; }
QCheckBox, QRadioButton { spacing: 7px; color: #344054; }
QCheckBox::indicator, QRadioButton::indicator { width: 16px; height: 16px; }
QTableWidget, QTableView {
    background: #ffffff;
    alternate-background-color: #f8faff;
    border: 1px solid #e1e7f0;
    border-radius: 9px;
    gridline-color: #edf0f5;
    selection-background-color: #dce6ff;
    selection-color: #172033;
}
QHeaderView::section {
    background: #f4f6fa;
    color: #667085;
    padding: 8px;
    border: none;
    border-bottom: 1px solid #e1e7f0;
    font-weight: 600;
}
QProgressBar {
    border: none;
    background: #e9edf4;
    border-radius: 3px;
    max-height: 6px;
    text-align: center;
}
QProgressBar::chunk { background: #4f7cff; border-radius: 3px; }
QSplitter::handle { background: transparent; width: 8px; height: 8px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 3px; }
QScrollBar::handle:vertical { background: #c4ccda; min-height: 28px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QStatusBar { background: #ffffff; border-top: 1px solid #e5eaf2; color: #667085; }
"""


APP_STYLESHEET = (
    _STYLESHEET_TEMPLATE
    .replace("__CHEVRON__", _asset_url("chevron-down.svg"))
    .replace("__CHEVRON_DISABLED__", _asset_url("chevron-down-disabled.svg"))
)
