# -*- coding: utf-8 -*-
"""PyInstaller 启动钩子：在任何 Qt 模块加载前选择软件渲染。"""

import os


if os.name == "nt":
    os.environ.setdefault("QT_OPENGL", "software")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
