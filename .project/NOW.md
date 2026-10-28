# NOW - Quick resume

## Current release checkpoint
- Project/path: cambida / `D:\1\cambida`; task: RELEASE-2.3.7-20261028.
- Revision: 9; updated_by: OpenCode; updated_at: 2026-10-28T17:35:44+07:00.
- Objective: ship 2.3.7 so installations at a drive root (for example `D:\`) can auto-update again without touching runtime data.
- Status: DONE; verification: VERIFIED for package, manifest/ZIP, regression suite, drive-root bridge regression and FixTool dry-run; not for real devices.
- Completed: rewrote the 2.3.4 compatibility bridge (`updater.ps1`) with a system-temp runner fallback for drive-root installs, CommandLineToArgvW argument quoting, restart-on-failure guarded by an `/api/ping` probe, and staging cleanup limited to the payload and `release.zip`; in `updater.exe`, allow same-drive staging for a drive-root target while still rejecting payloads inside a normal installation; added `tools/fix_cambida` (one-command repair: download, manifest verification, direct `updater.exe`, ping check, restart fallback) copied to `Desktop\Cambida-FixTool`; bumped the package to 2.3.7.
- Evidence: pytest **277 passed**; 3 bridge integration tests passed with the packaged `updater.exe` (drive-root target via `subst`, restart path, legacy payload); manifest 231 files 0 mismatch; ZIP 233 entries, CRC clean, 21 native modules, no `config.json`/database/key, no private `.py`; `Cambida.exe` FileVersion 2.3.7 and a rebuilt `updater.exe`; `updater.ps1` inside the ZIP is byte-identical to `scripts/universal_updater.ps1`; FixTool `-DryRun` verified all 231 files of `2.3.7.zip`.
- Release: tag `v2.3.7`, asset `2.3.7.zip` sha256 `713523282e35625a8c6c289cefab413f3bf4255b467e268811cfad978e3ee286` (228627288 bytes), schema 2 / kind full, not prerelease.
- Next: real-device/camera acceptance still not run; watch 2.3.4 drive-root machines upgrade through the hourly worker; machines that are already dead need `Desktop\Cambida-FixTool\Fix_Cambida.cmd`.
- Blockers/pending: none for this release; `tools/ai_client.py` still carries a committed API key that should be rotated.

## Earlier checkpoints (condensed)
- RELEASE-2.3.6-20261028: retired guest streaming, cleaned tree, native 2.3.6 package, tag `v2.3.6`; 271 tests, manifest 231 files.
- AUDIT-HARDEN-20261025: endpoint origin/session/LAN/privacy/canonical guards, camera lifecycle fixes, duplicate cleanup and isolated regressions; 63 Python passed, SOL high scoped ACCEPT. See [AUDIT_20261025.md](AUDIT_20261025.md).
- AGENTS-NORMALIZE-20261025: project instructions delegate to the global contract; device and test safeguards retained.
- Bootstrap snapshot 2026-10-22 remains historical and unverified.

## Immediate resume
- Verify Git status, actual files, tests and running processes before any work.
- Read HANDOFF.md and TASKS.md only as required for the active task.
