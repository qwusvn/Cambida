# NOW - Quick resume

## Current release checkpoint
- Project/path: cambida / `D:\1\cambida`; task: RELEASE-2.3.6-20261028.
- Revision: 8; updated_by: OpenCode; updated_at: 2026-10-28T01:10:00+07:00.
- Objective: ship the 2.3.6 native package after retiring the deprecated guest-streaming feature and purging its files, build output, runtime config and Git history.
- Status: DONE; verification: VERIFIED for package, regression suite and remote refs, not for real devices.
- Completed: removed the retired feature from the app, admin UI, services, tools, tests and docs; rebuilt the package from a clean tree; scrubbed leftover runtime config keys; squashed the post-2.3.5 work into one release commit; force-pushed main; recreated tag `v2.3.6`; expired reflogs and pruned local objects.
- Evidence: `pytest` **271 passed**; update-bridge integration passed with the packaged `updater.exe`; manifest 231 files 0 mismatch; archive 233 entries, CRC clean, 21 native modules, no private `.py`; `Cambida.exe` FileVersion 2.3.6; asset `sha256:1b4e15cc9b712b55f9385894ba58c984fcb45db80f19f3254cd9a573455a0e3b` (228624123 bytes) matches the GitHub digest; `git fsck` clean; reachable history has no retired-feature paths.
- Release: https://github.com/qwusvn/Cambida/releases/tag/v2.3.6 (Latest)
- Next: real-device/camera acceptance still not run; merge or discard worktree branch `fix/2.3.51-watchdog-bootloop`.
- Blockers/pending: GitHub may keep serving pre-squash commit SHAs until Support purges them, and existing clones/forks keep their own copies; `tools/ai_client.py` carries a committed API key that should be rotated.

## Earlier checkpoints (condensed)
- AUDIT-HARDEN-20261025: endpoint origin/session/LAN/privacy/canonical guards, camera lifecycle fixes, duplicate cleanup and isolated regressions; 63 Python passed, SOL high scoped ACCEPT. See [AUDIT_20261025.md](AUDIT_20261025.md).
- AGENTS-NORMALIZE-20261025: project instructions delegate to the global contract; device and test safeguards retained.
- Bootstrap snapshot 2026-10-22 remains historical and unverified.

## Immediate resume
- Verify Git status, actual files, tests and running processes before any work.
- Read HANDOFF.md and TASKS.md only as required for the active task.
