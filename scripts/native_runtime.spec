import json
import os
from pathlib import Path

root = Path(os.environ['CAMBIDA_BUILD_ROOT'])
settings = json.loads(Path(os.environ['CAMBIDA_RUNTIME_SETTINGS']).read_text(encoding='utf-8'))
a = Analysis(
    [str(root / 'scripts/native_launcher.py')],
    pathex=[],
    binaries=[(str(p), 'vendor/dahua_netsdk') for p in (root / 'vendor/dahua_netsdk').glob('*.dll')],
    datas=[(str(root / 'config.release.json'), '.')],
    hiddenimports=settings['imports'],
    excludes=settings['exclude'],
    hookspath=[], runtime_hooks=[], noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Cambida',
          console=False, debug=False, strip=False, upx=False,
          version=str(root / 'version_info_2_2_0.txt'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Cambida')
