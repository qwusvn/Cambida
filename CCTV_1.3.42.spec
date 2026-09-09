# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [
    (r'D:\1\cambida\index.html', '.'),
    (r'D:\1\cambida\home.html', '.'),
    (r'D:\1\cambida\live_all.html', '.'),
    (r'D:\1\cambida\timeline.html', '.'),
    (r'D:\1\cambida\admin.html', '.'),
    (r'D:\1\cambida\admin_login.html', '.'),
    (r'D:\1\cambida\stats.html', '.'),
]
binaries = []
hiddenimports = []
tmp_ret = collect_all('qrcode')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

a = Analysis(
    [r'D:\1\cambida\1.py'],
    pathex=[r'D:\1\cambida'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    a.binaries,
    a.datas,
    [],
    name='CCTV_1.3.42',
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
    version=r'D:\1\cambida\version_info_1_3_42.txt',
)
