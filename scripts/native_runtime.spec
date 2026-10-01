import json
import os
from pathlib import Path

root = Path(os.environ['CAMBIDA_BUILD_ROOT'])
settings = json.loads(Path(os.environ['CAMBIDA_RUNTIME_SETTINGS']).read_text(encoding='utf-8'))
hikvision_root = root / 'vendor/hikvision_netsdk'
hikvision_binaries = [
    (str(p), (Path('vendor/hikvision_netsdk') / p.parent.relative_to(hikvision_root)).as_posix())
    for p in hikvision_root.rglob('*.dll')
]
hikvision_datas = [
    (str(p), (Path('vendor/hikvision_netsdk') / p.parent.relative_to(hikvision_root)).as_posix())
    for p in hikvision_root.rglob('*') if p.is_file() and p.suffix.lower() != '.dll'
]
a = Analysis(
    [str(root / 'scripts/native_launcher.py')],
    pathex=[],
    binaries=[(str(p), 'vendor/dahua_netsdk') for p in (root / 'vendor/dahua_netsdk').glob('*.dll')] + hikvision_binaries,
    datas=[(str(root / 'config.release.json'), '.')] + hikvision_datas,
    hiddenimports=settings['imports'],
    excludes=settings['exclude'],
    hookspath=[], runtime_hooks=[], noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
version_file = os.environ.get('CAMBIDA_VERSION_FILE', 'version_info_2_2_2.txt')
if not (root / version_file).exists():
    version_file = 'version_info_2_2_0.txt'
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Cambida',
          console=False, debug=False, strip=False, upx=False,
          version=str(root / version_file))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Cambida')
