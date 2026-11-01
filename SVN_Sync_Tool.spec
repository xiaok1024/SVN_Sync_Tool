# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['svn_sync_qt.py'],
    pathex=[],
    binaries=[],
    # 样式表用绝对路径引用 qt_assets 下的 SVG 图标，单文件模式会解包到
    # sys._MEIPASS，qt_theme._asset_url 据此拼路径，这里必须一并打包。
    datas=[('qt_assets', 'qt_assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['pyi_rth_qt_software.py'],
    excludes=['tkinter', 'ttkbootstrap'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SVN_Sync_Tool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
