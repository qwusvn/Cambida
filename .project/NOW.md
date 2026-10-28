# NOW - Quick resume

## Current release checkpoint
- Project/path: cambida / `D:\1\cambida`; task: RELEASE-2.3.8-20261028.
- Revision: 11; updated_by: OpenCode; updated_at: 2026-10-28T18:40:00+07:00.
- Objective: ship 2.3.8 with a Windows sleep blocker and an automatic firewall exception, without touching runtime data.
- Status: DONE; verification: VERIFIED for package, manifest/ZIP, compiled module loading, regression suite and netsh rule detection against a real rule; not for real devices.
- Completed: added the compiled `windows_integration` module; a dedicated thread holds `ES_CONTINUOUS | ES_SYSTEM_REQUIRED` while the app runs, retries failed assertions and releases the state on quit; the frozen executable is added to the inbound firewall list as a program rule (`Cambida CCTV`), silently when elevated or through one UAC request for administrator accounts, with a declined-request marker in `%LOCALAPPDATA%\Cambida\firewall-elevation.json`; wired both into startup and the tray quit path; bumped to 2.3.8.
- Evidence: pytest **301 passed**; `windows_integration.pyd` loads from the package and exposes the API; `netsh ... show rule ... verbose` detection matched a real program rule and rejected a wrong path; manifest 232 files 0 mismatch; ZIP 234 entries CRC clean, 22 native modules, no config/database/key, no private `.py`; `Cambida.exe` FileVersion 2.3.8; BUILD_INFO clean (`fb0a30c`).
- Release: https://github.com/qwusvn/Cambida/releases/tag/v2.3.8 (Latest), asset `2.3.8.zip` sha256 `e36e712266e13e841fca7a3acff46eaf97ee0eb4cc55d51e3e01852ce05805b9` (228705692 bytes), schema 2 / kind full, not prerelease.
- Next: real-device acceptance still not run; verify on a shop PC that the screen may turn off while recording continues, and that the first start on an unelevated administrator asks for UAC once and then shows the `Cambida CCTV` inbound rule.
- Blockers/pending: none for this release; `tools/ai_client.py` still carries a committed API key that should be rotated.

## Earlier checkpoints (condensed)
- RELEASE-2.3.7-20261028: drive-root bridge fix, root-target staging in updater.exe, `Cambida-FixTool` for dead machines; 277 tests, tag `v2.3.7`.
- RELEASE-2.3.6-20261028: retired guest streaming, cleaned tree, native 2.3.6 package, tag `v2.3.6`; 271 tests.
- AUDIT-HARDEN-20261025: endpoint origin/session/LAN/privacy/canonical guards, camera lifecycle fixes, duplicate cleanup and isolated regressions; 63 Python passed, SOL high scoped ACCEPT. See [AUDIT_20261025.md](AUDIT_20261025.md).
- AGENTS-NORMALIZE-20261025: project instructions delegate to the global contract; device and test safeguards retained.
- Bootstrap snapshot 2026-10-22 remains historical and unverified.

## Immediate resume
- Verify Git status, actual files, tests and running processes before any work.
- Read HANDOFF.md and TASKS.md only as required for the active task.
