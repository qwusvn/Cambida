"""Cambida 2.3+ full-release archive validation.

The update protocol is intentionally independent from the application layout.
A release declares every program-owned file plus a start entrypoint in
update.json. Runtime/shop data is never allowed inside a release archive.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import urllib.parse
import zipfile

PROTECTED = {
    'config.json', 'analytics.db', 'device_id.key', 'tunnel_token.txt',
    'controlhub_machine_id.txt', 'controlhub_client_secret.txt', 'controlhub_bootstrap.json',
    'logs', 'cctv_videos', 'nvr_cache', '.git', '.project', '.updates',
    '.update-backups', '.pending_update_notification', 'native-update.log',
}


def safe_path(root, name):
    path = PurePosixPath(name)
    if (
        not name
        or '\\' in name
        or ':' in name
        or path.is_absolute()
        or any(
            p in {'.', '..'}
            or p.casefold() in PROTECTED
            or p.endswith((' ', '.'))
            for p in path.parts
        )
    ):
        raise ValueError('Unsafe update path: ' + name)
    target = (Path(root) / name).resolve()
    if not target.is_relative_to(Path(root).resolve()):
        raise ValueError('Update path escapes its directory')
    return target


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _validate_local_health(update):
    health = update.get('health_v2') or update.get('health')
    if not health:
        return
    if not isinstance(health, dict):
        raise ValueError('Health metadata is invalid')

    timeout = int(health.get('timeout_seconds') or 60)
    if timeout < 5 or timeout > 300:
        raise ValueError('Health timeout is out of range')

    mode = str(health.get('mode') or '').strip().lower()
    if mode == 'config_port':
        path = str(health.get('path') or '/').strip()
        if not path.startswith('/') or '://' in path:
            raise ValueError('Health path is invalid')
        return

    raw = str(health.get('url') or '').strip()
    if not raw:
        raise ValueError('Health URL is empty')
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('Health URL must use local HTTP')


def prepare_archive(archive, destination, installed, version):
    """Extract and validate one full schema-2 release.

    The installed path is accepted for API compatibility but validation of a
    full release does not depend on the previous application architecture.
    """
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid release version')
    destination = Path(destination)
    installed = Path(installed)

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
                raise ValueError('Not a Cambida 2.3+ release: missing ' + required)
        bundle.extractall(destination)

    manifest = _load_json(destination / 'release_manifest.json')
    update = _load_json(destination / 'update.json')
    if manifest.get('schema') != 2 or update.get('schema') != 2:
        raise ValueError('Unsupported update schema')
    if manifest.get('version') != version or update.get('version') != version:
        raise ValueError('Release version mismatch')
    if update.get('kind') != 'full':
        raise ValueError('Cambida 2.3+ updater accepts full releases only')

    expected = manifest.get('files')
    if not isinstance(expected, dict) or not expected:
        raise ValueError('Release manifest contains no files')

    start = update.get('start')
    if not isinstance(start, dict):
        raise ValueError('Release start entrypoint is missing')
    start_path = str(start.get('path') or '').strip()
    if not start_path or start_path not in expected:
        raise ValueError('Release start entrypoint must be listed in the manifest')
    safe_path(destination, start_path)
    args = start.get('args', [])
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError('Release start arguments must be strings')
    _validate_local_health(update)

    for name, checksum in expected.items():
        if not isinstance(name, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', str(checksum)):
            raise ValueError('Invalid release manifest entry')
        payload = safe_path(destination, name)
        if not payload.is_file() or sha256(payload) != str(checksum).lower():
            raise ValueError('Release checksum mismatch: ' + name)

    allowed = set(expected) | {'update.json', 'release_manifest.json'}
    for path in destination.rglob('*'):
        if path.is_file() and path.relative_to(destination).as_posix() not in allowed:
            raise ValueError('Unlisted release file: ' + path.relative_to(destination).as_posix())

    # A full release must never carry local/shop state, even if a malicious
    # manifest attempted to list it.
    for name in expected:
        safe_path(installed, name)

    return {'manifest': manifest, 'update': update}
