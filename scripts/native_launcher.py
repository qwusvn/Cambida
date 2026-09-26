"""Stable frozen host. Application code lives only in external native modules."""
import ctypes
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import sys


class NativeModules(importlib.abc.MetaPathFinder):
    def __init__(self, root):
        self.root = root.resolve()
        self.entries = json.loads((root / "modules.json").read_text(encoding="utf-8"))

    def find_spec(self, fullname, path=None, target=None):
        entry = self.entries.get(fullname)
        if entry is None:
            return None
        binary = (self.root / entry["file"]).resolve()
        if not binary.is_relative_to(self.root) or binary.suffix != ".pyd":
            raise ImportError("Invalid native module path: " + fullname)
        if not binary.is_file():
            raise ImportError("Missing native module: " + fullname)
        locations = [str(binary.parent / fullname.rsplit(".", 1)[-1])] if entry["package"] else None
        return importlib.util.spec_from_file_location(
            fullname, binary, submodule_search_locations=locations
        )


def main():
    root = Path(sys.executable).resolve().parent
    sdk = Path(getattr(sys, '_MEIPASS', root)) / 'vendor/dahua_netsdk'
    os.environ.setdefault('DAHUA_NETSDK_DIR', str(sdk))
    sys.meta_path.insert(0, NativeModules(root / "modules"))
    # Dynamic import is intentional: PyInstaller must never embed a second copy.
    app = __import__("cambida_app")
    app.main()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        root = Path(sys.executable).resolve().parent
        error = traceback.format_exc()
        try:
            (root / "startup-error.log").write_text(error, encoding="utf-8")
        except OSError:
            pass
        ctypes.windll.user32.MessageBoxW(
            None, "Khong the khoi dong Cambida. Xem startup-error.log trong thu muc ung dung.",
            "Cambida", 0x10,
        )
        raise
