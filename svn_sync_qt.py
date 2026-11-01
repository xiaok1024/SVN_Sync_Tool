#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SVN 代码同步工具的现代 Windows GUI 入口。"""

import os
import sys

# Parallels/部分旧 Windows 显卡驱动的 Qt 硬件合成会产生黑屏；Widgets 不依赖 GPU，
# Windows 默认使用软件 OpenGL 可获得稳定显示与截图。用户显式设置时尊重其选择。
if sys.platform == "win32":
    os.environ.setdefault("QT_OPENGL", "software")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication
except ImportError:
    sys.stderr.write(
        "缺少 PySide6-Essentials，无法启动现代 GUI。请在开发环境安装 requirements.txt；"
        "终端版仍可运行 python3 svn_sync_cli.py。\n")
    raise SystemExit(1)

from qt_app import MainWindow


def main(argv=None):
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(list(sys.argv if argv is None else argv))
    app.setApplicationName("SVN Sync Tool")
    app.setOrganizationName("Ecology Tooling")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
