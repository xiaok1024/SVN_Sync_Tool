# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all


ttkbootstrap_datas, ttkbootstrap_binaries, ttkbootstrap_hiddenimports = collect_all('ttkbootstrap')

a = Analysis(
    ['svn_sync_tool.py'],
    pathex=[],
    binaries=ttkbootstrap_binaries,
    datas=ttkbootstrap_datas,
    hiddenimports=ttkbootstrap_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SVN_Sync_Tool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SVN_Sync_Tool',
)
app = BUNDLE(
    coll,
    name='SVN_Sync_Tool.app',
    icon=None,
    bundle_identifier=None,
)
