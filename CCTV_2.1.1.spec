import os
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(SPECPATH)

# Các file giao diện và ffmpeg.exe được phát hành cạnh EXE để dễ thay thế.
# Chỉ giữ config mặc định bên trong _internal làm bản seed nếu config.json cạnh EXE bị thiếu.
datas = [
    (os.path.join(ROOT, 'config.json'), '.'),
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
    name='CCTV_2.1.1',
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
    version=os.path.join(ROOT, 'version_info_2_1_1.txt'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CCTV_2.1.1',
)
