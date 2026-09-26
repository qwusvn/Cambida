"""Build native modules incrementally and a reusable PyInstaller runtime on D:.

This builds release artifacts only; it never imports or launches Cambida.
"""
import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'staging/native-build'
PYD = BUILD / 'modules'
PACKAGES = ('camera_modules', 'services', 'routes')
DIRECTIVES = dict(language_level=3, binding=True, annotation_typing=False,
                  infer_types=False, embedsignature=False)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def read_json(path, default=None):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else default


def key(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def run(argv, **kwargs):
    print('RUN:', ' '.join(str(arg) for arg in argv), flush=True)
    subprocess.run(argv, check=True, cwd=ROOT, **kwargs)


def source_modules():
    result = {'cambida_app': (ROOT / '1.py', False),
              'native_update': (ROOT / 'native_update.py', False),
              'dahua_37777': (ROOT / 'dahua_37777.py', False)}
    for package in PACKAGES:
        for path in sorted((ROOT / package).rglob('*.py')):
            parts = list(path.relative_to(ROOT).with_suffix('').parts)
            is_package = parts[-1] == '__init__'
            if is_package:
                parts.pop()
            result['.'.join(parts)] = (path, is_package)
    return result


def compiler_environment():
    # Existing Build Tools installations may be usable even if vswhere reports
    # an incomplete optional workload. Never install or modify Visual Studio.
    roots = [Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft Visual Studio',
             Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Microsoft Visual Studio']
    candidates = [p for root in roots for p in root.glob('*/*/VC/Auxiliary/Build/vcvars64.bat')]
    if not candidates:
        raise RuntimeError('MSVC x64 build tools are required')
    vcvars = sorted(candidates)[-1]
    wrapper = BUILD / 'compiler-env.cmd'
    wrapper.write_text('@echo off\ncall "' + str(vcvars) + '" >nul\nif errorlevel 1 exit /b 1\nset\n', encoding='utf-8')
    result = subprocess.run('cmd.exe /d /s /c ""' + str(wrapper) + '""', capture_output=True, check=True)
    env = dict(os.environ)
    for line in result.stdout.decode('mbcs', errors='replace').splitlines():
        name, separator, value = line.partition('=')
        if separator and name:
            env[name] = value
    env['DISTUTILS_USE_SDK'] = '1'
    env['MSSdk'] = '1'
    return env


def compile_modules(modules):
    PYD.mkdir(parents=True, exist_ok=True)
    generated = BUILD / 'generated'
    generated.mkdir(exist_ok=True)
    abi = sysconfig.get_config_var('EXT_SUFFIX')
    previous = read_json(BUILD / 'compile-cache.json', {})
    compiler_env = compiler_environment()
    toolchain = dict(python=sys.version, cython=importlib.metadata.version('Cython'),
                     setuptools=importlib.metadata.version('setuptools'), directives=DIRECTIVES,
                     msvc=compiler_env.get('VCToolsVersion'), flags=['/O1', '/GL-'])
    current, changed, entries = {}, [], {}
    for name, (source, package) in modules.items():
        output = PYD / (name.replace('.', '/') + abi)
        fingerprint = key({'source': digest(source), 'name': name, 'toolchain': toolchain})
        cached = previous.get(name, {})
        if cached.get('input') != fingerprint or not output.exists() or cached.get('output') != digest(output):
            # Use a generated copy for 1.py so the extension has a valid name;
            # no generated C or source copy is shipped to customers.
            copy = generated / (name.replace('.', '/') + '.py')
            copy.parent.mkdir(parents=True, exist_ok=True)
            if not copy.exists() or digest(copy) != digest(source):
                shutil.copy2(source, copy)
            changed.append((name, str(copy)))
        current[name] = {'input': fingerprint, 'output': None}
        entries[name] = {'file': output.relative_to(PYD).as_posix(), 'package': package}
    if changed:
        setup = BUILD / 'compile_modules.py'
        setup.write_text(
            'from setuptools import setup, Extension\nfrom Cython.Build import cythonize\n'
            + 'extensions = [Extension(n, [p], extra_compile_args=["/O1", "/GL-"]) for n,p in '
            + repr(changed) + ']\n'
            + 'setup(name="cambida-native", ext_modules=cythonize(extensions, compiler_directives='
            + repr(DIRECTIVES) + ', build_dir=' + repr(str(BUILD / 'cython')) + '))\n', encoding='utf-8')
        print(f'Compiling {len(changed)}/{len(modules)} native modules', flush=True)
        run([sys.executable, str(setup), 'build_ext', '--build-lib', str(PYD),
             '--build-temp', str(BUILD / 'objects'), '--parallel', '2'], env=compiler_env)
    else:
        print('All native modules unchanged: using cached binaries', flush=True)
    for name in current:
        output = PYD / entries[name]['file']
        if not output.is_file():
            raise RuntimeError(f'Missing compiled module: {name}')
        current[name]['output'] = digest(output)
    write_json(BUILD / 'compile-cache.json', current)
    return entries


def runtime(modules):
    owned = set(modules)
    owned_roots = {name.split('.')[0] for name in owned}
    imports = {'pystray._win32', 'qrcode.image.pil', 'waitress', 'PIL.Image', 'PIL.ImageDraw'}
    for source, _ in modules.values():
        for node in ast.walk(ast.parse(source.read_text(encoding='utf-8-sig'))):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                imports.add(node.module)
    imports = sorted(name for name in imports if name.split('.')[0] not in owned_roots)
    settings = {'imports': imports, 'exclude': sorted(owned_roots)}
    settings_file = BUILD / 'runtime-settings.json'
    write_json(settings_file, settings)
    # Runtime changes only with loader, Python, dependencies, SDK, or seed config.
    inputs = [ROOT / 'scripts/native_launcher.py', ROOT / 'scripts/native_runtime.spec',
              ROOT / 'version_info_2_2_0.txt', ROOT / 'config.release.json',
              *sorted((ROOT / 'vendor/dahua_netsdk').glob('*.dll'))]
    fingerprint = key({'settings': settings, 'python': sys.version,
                       'dependencies': sorted((d.metadata['Name'], d.version)
                                              for d in importlib.metadata.distributions()),
                       'files': {p.name: digest(p) for p in inputs}})
    out = BUILD / 'runtime-dist/Cambida'
    cache = read_json(BUILD / 'runtime-cache.json', {})
    runtime_files = cache.get('files', {})
    cache_valid = cache.get('input') == fingerprint and bool(runtime_files) and all(
        (out / p).is_file() and digest(out / p) == checksum for p, checksum in runtime_files.items())
    if not cache_valid:
        env = dict(os.environ, CAMBIDA_BUILD_ROOT=str(ROOT), CAMBIDA_RUNTIME_SETTINGS=str(settings_file))
        run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', str(out.parent),
             '--workpath', str(BUILD / 'runtime-work'), str(ROOT / 'scripts/native_runtime.spec')], env=env)
        # PyInstaller's analysis TOCs must contain no private application bytecode.
        for toc in (BUILD / 'runtime-work').rglob('PYZ-00.toc'):
            data = toc.read_text(encoding='utf-8')
            for name in owned:
                if repr(name) + ',' in data:
                    raise RuntimeError(f'Private module bundled as bytecode: {name}')
        runtime_files = {p.relative_to(out).as_posix(): digest(p) for p in out.rglob('*') if p.is_file()}
        write_json(BUILD / 'runtime-cache.json', {'input': fingerprint, 'files': runtime_files})
    else:
        print('Runtime unchanged: reusing Cambida.exe and _internal', flush=True)
    return out, fingerprint


def make_zip(folder, target, files=None):
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(files or [p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()]):
            archive.write(folder / path, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--base-release', type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
        parser.error('Version must be X.Y.Z')
    destination = ROOT / 'release' / args.version
    archive = destination.parent / (args.version + '.zip')
    if destination.exists() or archive.exists():
        raise RuntimeError(f'Refusing to overwrite an existing release: {destination}')
    release = BUILD / ('package-' + args.version + '-' + uuid.uuid4().hex[:8])
    modules = source_modules()
    sidecars = ('index.html', 'admin.html', 'admin_login.html', 'home.html', 'live_all.html',
                'stats.html', 'timeline.html', 'ffmpeg.exe', 'cloudflared.exe')
    cloud_files = ('HD_SU_DUNG.txt', 'install_tunnel.bat', 'run_tunnel_silent.vbs',
                   'setup_tunnel.ps1', 'start_tunnel.ps1', 'stop_tunnel.bat')
    inputs = [p for p, _ in modules.values()] + [ROOT / name for name in sidecars]
    inputs += [ROOT / 'tools/cloudflared_setup' / name for name in cloud_files]
    inputs += [ROOT / name for name in ('scripts/native_launcher.py', 'scripts/native_runtime.spec',
               'scripts/native_updater.cmd', 'scripts/native_updater.ps1',
               'scripts/release_launcher.cmd', 'config.release.json', 'NATIVE_RELEASE.md')]
    snapshot = {str(p.relative_to(ROOT)): digest(p) for p in inputs}
    entries = compile_modules(modules)
    runtime_dir, runtime_id = runtime(modules)
    release.mkdir(parents=True)
    shutil.copytree(runtime_dir, release, dirs_exist_ok=True)
    for entry in entries.values():
        dest = release / 'modules' / entry['file']
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PYD / entry['file'], dest)
    write_json(release / 'modules/modules.json', entries)
    for filename in sidecars:
        shutil.copy2(ROOT / filename, release / filename)
    # Explicit allowlist: no credentials, runtime configuration, media or source.
    cloud_dest = release / 'cloudflared_setup'
    cloud_dest.mkdir()
    for filename in cloud_files:
        shutil.copy2(ROOT / 'tools/cloudflared_setup' / filename, cloud_dest / filename)
    for filename in ('RELEASE_VERSION.txt', 'VERSION.txt'):
        (release / filename).write_text(args.version + '\n', encoding='utf-8')
    shutil.copy2(ROOT / 'scripts/release_launcher.cmd', release / 'Chay_CCTV.cmd')
    shutil.copy2(ROOT / 'scripts/native_updater.cmd', release / 'updater.cmd')
    shutil.copy2(ROOT / 'scripts/native_updater.ps1', release / 'native_updater.ps1')
    shutil.copy2(ROOT / 'NATIVE_RELEASE.md', release / 'README_RELEASE.md')
    write_json(release / 'BUILD_INFO.json', {
        'version': args.version, 'source': snapshot, 'runtime_id': runtime_id,
        'python': sys.version, 'cython': importlib.metadata.version('Cython'),
        'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)),
        'tests': 'NOT RUN - not requested', 'device_acceptance': 'NOT RUN - not requested',
    })
    for path, checksum in snapshot.items():
        if digest(ROOT / path) != checksum:
            raise RuntimeError(f'Source changed during build: {path}; package not released')
    forbidden = {'config.json', 'analytics.db', 'device_id.key', 'logs', 'cctv_videos', 'nvr_cache'}
    for path in release.rglob('*'):
        # Third-party runtime packages such as OpenCV need their bootstrap .py
        # files. Our own application code must exist only as native modules.
        private_source = path.relative_to(release).parts[0] != '_internal'
        if path.name in forbidden or (private_source and path.is_file() and path.suffix.lower() in {'.py', '.pyc', '.pyx', '.c'}):
            raise RuntimeError(f'Forbidden release content: {path.relative_to(release)}')
    manifest = {'schema': 1, 'version': args.version, 'runtime_id': runtime_id,
                'abi': sys.implementation.cache_tag + '-' + sysconfig.get_platform(),
                'files': {p.relative_to(release).as_posix(): digest(p)
                          for p in sorted(release.rglob('*')) if p.is_file()}}
    write_json(release / 'release_manifest.json', manifest)
    write_json(release / 'update.json', {'schema': 1, 'kind': 'full', 'version': args.version})
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive_temp = BUILD / (args.version + '-' + uuid.uuid4().hex[:8] + '.zip')
    make_zip(release, archive_temp)
    release.rename(destination)
    archive_temp.rename(archive)
    release = destination
    print(f'RELEASE={release}\nZIP={archive}\nSHA256={digest(archive)}', flush=True)
    if args.base_release:
        base = args.base_release.resolve()
        old = read_json(base / 'release_manifest.json')
        if old['abi'] != manifest['abi'] or old['runtime_id'] != runtime_id:
            raise RuntimeError('Runtime or ABI changed: full package built; delta refused')
        if set(old['files']) - set(manifest['files']):
            raise RuntimeError('File removals require a full release; delta refused')
        changed = [p for p, checksum in manifest['files'].items() if old['files'].get(p) != checksum]
        delta_folder = BUILD / ('delta-' + args.version)
        delta_folder.mkdir(exist_ok=False)
        for path in changed + ['release_manifest.json']:
            target = delta_folder / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(release / path, target)
        write_json(delta_folder / 'update.json', {
            'schema': 1, 'kind': 'delta', 'version': args.version,
            'base_version': old['version'], 'base_manifest_sha256': digest(base / 'release_manifest.json'),
        })
        patch = release.parent / f'{args.version}-from-{old["version"]}.patch.zip'
        make_zip(delta_folder, patch)
        print(f'PATCH={patch}\nPATCH_SHA256={digest(patch)}', flush=True)


if __name__ == '__main__':
    main()
