# Cambida 2.2 native release

## Development and packaging

Run `python 1.py` for source development as before. Do not put release `.pyd`
files in the source directories. The release host `Cambida.exe` loads only the
native application modules listed in `modules/modules.json`.

Build a full release with PowerShell:

```powershell
.\package_2.2.0.ps1
```

Outputs: `release/2.2.0/` and `release/2.2.0.zip`. Build caches, generated C,
temporary files and the build virtual environment stay under
`D:\1\cambida\staging\native-build`. Requires CPython 3.13 x64, MSVC x64,
Cython 3.1.6, PyInstaller and the application requirements. The build uses the
existing installed runtime dependencies; `BUILD_INFO.json` records provenance.

For a later release, edit the relevant Python module or HTML file, then run:

```powershell
.\package_2.2.0.ps1 -Version 2.2.1 -BaseRelease .\release\2.2.0
```

The builder hashes each source and reuses unchanged native modules. It reuses
the EXE/runtime when the loader, Python, installed dependencies, SDK and seed
configuration are unchanged. The optional delta contains changed files only;
its filename is `2.2.1-from-2.2.0.patch.zip`. Changed runtime/ABI or removed files
requires the full ZIP. Keep the base release directory to create future deltas.
Do not hand-edit release manifests or mix module files from different builds.

`1.py` remains the application source and is compiled into `cambida_app.pyd`.
Existing `services`, `routes`, `camera_modules`, `dahua_37777` and the native
update validator compile independently. Further splitting of application logic
can be done gradually; this release does not rewrite all route/worker state.

## Installing on another Windows x64 PC

Extract the complete folder and launch `Chay_CCTV.cmd` / `Cambida.exe`.
Python is bundled; no separate Python installation is needed. Application
source, operational configuration, credentials, video and database are not
copied into the release. On first launch, a safe config seed is used.

Cloudflared and FFmpeg are included beside the EXE. The `cloudflared_setup`
folder provides the existing interactive tunnel setup. Packaging does not
create a tunnel, install a service or transfer this machine's tunnel credentials.

For migration from 2.1.x, fully exit that installation, back it up, then copy
the full 2.2.0 package into its directory and launch the new `Chay_CCTV.cmd`.
Preserve `config.json`, `analytics.db`, `device_id.key`, media and other runtime
data. Do not use the old updater for this one-time transition: its legacy
cleanup can terminate unrelated FFmpeg/Cloudflared processes. Existing tunnel
services must be handled separately if their executable needs replacing.

From 2.2 onward the application selects a delta for its exact base version or
the full `<version>.zip` asset. The updater verifies the manifest and SHA-256,
waits for its owning application to exit, copies changed files, and backs up
replaced files under `.update-backups`. Copy failures restore previous files.
It does not terminate tunnel services or unrelated recording processes.
File backups support recovery; this is not a guarantee of crash/power-loss
atomicity or automatic rollback after an application-level startup failure.

Publish the full ZIP for every release; optionally attach the delta as a second
asset. Old 2.1 clients must migrate manually and must not auto-install patches.
Hashes detect corrupt/mismatched files, not a malicious release publisher;
updates retain the existing trusted GitHub/HTTPS distribution channel.

Native compilation raises the cost of reverse engineering; it does not make
embedded credentials secret or provide unbreakable license protection.
The stable host's Windows file version is 2.2.0; the product version is read from
`RELEASE_VERSION.txt` so patch updates do not require rebuilding the host.

Tests and device/runtime acceptance: NOT RUN unless separately requested.
