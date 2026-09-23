import os
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(SPECPATH)

# Release seed only: never embed the operational config.json.
# The app copies this safe template to BASE_DIR/config.json only when that file is absent.
datas = [
    (os.path.join(ROOT, 'config.release.json'), '.'),
    (os.path.join(ROOT, 'RELEASE_VERSION.txt'), '.'),
]

binaries = [
    (os.path.join(ROOT, 'vendor', 'dahua_netsdk', '*.dll'), 'vendor/dahua_netsdk'),
]
hiddenimports = []

tmp_ret = collect_all('qrcode')
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

a = Analysis(
    [os.path.join(ROOT, '1.py')],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=True,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CCTV_2.1.2',
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
    version=os.path.join(ROOT, 'version_info_2_1_2.txt'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CCTV_2.1.2',
)
