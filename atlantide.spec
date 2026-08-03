# -*- mode: python ; coding: utf-8 -*-

# Path is relative to the spec file's directory, which PyInstaller sets as the
# CWD for Analysis. An absolute path here builds only on the machine that ran
# `pyi-makespec`.
a = Analysis(
    ['atlantide/cli/main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# One-file build: binaries and datas go inside the executable rather than
# beside it, so `dist/atlantide` is a single file the release job can upload
# as-is. A COLLECT/onedir build would make `dist/atlantide` a directory and
# the workflow's `cp` would fail.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='atlantide',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
