"""Release archive validation; no application state or network side effects."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

PROTECTED = {'config.json', 'analytics.db', 'device_id.key', 'logs', 'cctv_videos',
             'nvr_cache', '.git', '.project', '.update-backups'}


def safe_path(root, name):
    path = PurePosixPath(name)
    if (not name or '\\' in name or ':' in name or path.is_absolute()
            or any(p in {'.', '..'} or p.lower() in PROTECTED or p.endswith((' ', '.')) for p in path.parts)):
        raise ValueError('Unsafe update path: ' + name)
    target = (Path(root) / name).resolve()
    if not target.is_relative_to(Path(root).resolve()):
        raise ValueError('Update path escapes its directory')
    return target


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def prepare_archive(archive, destination, installed, version):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid release version')
    destination, installed = Path(destination), Path(installed)
    with zipfile.ZipFile(archive) as bundle:
        seen = set()
        for entry in bundle.infolist():
            safe_path(destination, entry.filename)
            folded = entry.filename.rstrip('/').casefold()
            if folded in seen:
                raise ValueError('Duplicate archive path')
            seen.add(folded)
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Symlinks are not allowed in updates')
        for required in ('release_manifest.json', 'update.json'):
            if required not in bundle.namelist():
                raise ValueError('Not a native Cambida release: missing ' + required)
        bundle.extractall(destination)
    manifest = json.loads((destination / 'release_manifest.json').read_text(encoding='utf-8'))
    update = json.loads((destination / 'update.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != 1 or update.get('schema') != 1:
        raise ValueError('Unsupported update schema')
    if manifest.get('version') != version or update.get('version') != version:
        raise ValueError('Release version mismatch')
    if update.get('kind') not in {'full', 'delta'}:
        raise ValueError('Unknown update kind')
    if update['kind'] == 'delta':
        old_manifest = installed / 'release_manifest.json'
        if not old_manifest.is_file() or sha256(old_manifest) != update.get('base_manifest_sha256'):
            raise ValueError('Delta does not match installed release; use full package')
        old = json.loads(old_manifest.read_text(encoding='utf-8'))
        if old.get('version') != update.get('base_version') or old.get('abi') != manifest.get('abi'):
            raise ValueError('Delta base version/ABI mismatch')
        if old.get('runtime_id') != manifest.get('runtime_id'):
            raise ValueError('Runtime change requires full release')
    expected = manifest.get('files', {})
    if not {'Cambida.exe', 'modules/modules.json', 'RELEASE_VERSION.txt'} <= expected.keys():
        raise ValueError('Incomplete release manifest')
    for name, checksum in expected.items():
        payload = safe_path(destination, name)
        current = safe_path(installed, name)
        candidate = payload if payload.is_file() else current if update['kind'] == 'delta' else payload
        if not candidate.is_file() or sha256(candidate) != checksum:
            raise ValueError('Release checksum mismatch: ' + name)
    allowed = set(expected) | {'update.json', 'release_manifest.json'}
    for path in destination.rglob('*'):
        if path.is_file() and path.relative_to(destination).as_posix() not in allowed:
            raise ValueError('Unlisted release file')
    return manifest
