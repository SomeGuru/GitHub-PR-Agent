# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for GitHub PR Agent.
# Build locally with:  pyinstaller --noconfirm GitHub_PR_Agent.spec
# The GitHub Actions workflow uses the equivalent one-line pyinstaller command.

block_cipher = None

a = Analysis(
    ['GitHub_PR_Agent.py'],
    pathex=[],
    binaries=[],
    datas=[],
    # These stdlib modules are used by the encrypted PAT vault and are imported
    # indirectly, so declare them explicitly for a windowed build.
    hiddenimports=[
        'hmac',
        'base64',
        'secrets',
        'hashlib',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='GitHub_PR_Agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
