import os
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(SPECPATH)

datas = [
    (os.path.join(ROOT, 'config.release.json'), '.'),
    (os.path.join(ROOT, 'RELEASE_VERSION.txt'), '.'),
]

binaries = [
    (os.path.join(ROOT, 'vendor', 'dahua_netsdk', f), 'vendor/dahua_netsdk')
    for f in os.listdir(os.path.join(ROOT, 'vendor', 'dahua_netsdk'))
    if f.lower().endswith('.dll')
]

# Hikvision HCNetSDK contains a root DLL set plus HCNetSDKCom dependencies.
# Preserve the vendor directory layout because HCNetSDK loads companions at runtime.
hik_sdk_root = os.path.join(ROOT, 'vendor', 'hikvision_netsdk')
if os.path.isdir(hik_sdk_root):
    for current_root, _, files in os.walk(hik_sdk_root):
        rel_dir = os.path.relpath(current_root, ROOT).replace('\\', '/')
        for filename in files:
            src = os.path.join(current_root, filename)
            lower = filename.lower()
            if lower.endswith('.dll'):
                binaries.append((src, rel_dir))
            elif lower.endswith('.xml'):
                datas.append((src, rel_dir))

import ast
import glob

def extract_project_imports(filepath):
    mods = set()
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    mods.add(n.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    mods.add(node.module)
    except Exception:
        pass
    return mods

auto_hidden = set()
scan_files = (
    [os.path.join(ROOT, '1.py'), os.path.join(ROOT, 'dahua_37777.py'), os.path.join(ROOT, 'native_update.py')]
    + glob.glob(os.path.join(ROOT, 'camera_modules', '*.py'))
)
for sf in scan_files:
    auto_hidden.update(extract_project_imports(sf))

hiddenimports = list(auto_hidden) + [
    'logging.handlers',
    'concurrent.futures',
    'dataclasses',
    'xml.etree.ElementTree',
    'urllib.parse',
    'urllib.request',
    'urllib.error',
    'sqlite3',
    'winreg',
    'zipfile',
    'queue',
    'hmac',
    'hashlib',
    'ipaddress',
    'secrets',
    'stat',
    'webbrowser',
    'ctypes',
    'dahua_37777',
    'native_update',
    'camera_modules',
    'camera_modules.core',
    'camera_modules.config',
    'camera_modules.dahua',
    'camera_modules.discovery',
    'camera_modules.hikvision',
    'camera_modules.onvif',
    'camera_modules.policy',
    'camera_modules.rtsp',
    'camera_modules.table_access',
    'psutil',
    'waitress',
    'pystray',
    'PIL',
    'PIL.Image',
    'PIL.ImageDraw',
    'requests',
    'requests.auth',
    'qrcode',
    'flask',
    'cv2',
]

for pkg in ('qrcode', 'flask', 'waitress', 'camera_modules', 'requests'):
    tmp_ret = collect_all(pkg)
    datas += tmp_ret[0]
    binaries += tmp_ret[1]
    hiddenimports += tmp_ret[2]

a = Analysis(
    [os.path.join(ROOT, 'scripts', 'camhl_launcher.py')],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['config.json', 'analytics.db'],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='camhl',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=os.path.join(ROOT, 'version_info_2_2_3.txt'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='camhl',
)
