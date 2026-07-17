# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['GitHub_PR_Agent.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['json', 're', 'time', 'shutil', 'threading', 'traceback', 'webbrowser', 'subprocess', 'urllib', 'urllib.request', 'urllib.error', 'pathlib', 'datetime', 'tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox', 'tkinter.scrolledtext'],
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
    name='GitHub_PR_Agent',
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
    name='GitHub_PR_Agent',
)
