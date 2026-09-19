# HANDOFF — Cambida

Cập nhật: 2026-09-12 +07

## Bắt đầu hội thoại/agent mới
1. Workspace: `D:\1\cambida`.
2. Đọc `.project/PROJECT.md` → `DECISIONS.md` → `STATE.md` → `TASKS.md` → `HANDOFF.md`.
3. Đối chiếu source + Git; source/Git là source of truth.
4. Có `.codegraph` thì dùng CodeGraph trước task coding/phân tích lớn.
5. Mỗi task hoàn tất phải có commit riêng đúng scope; không gom thay đổi cũ ngoài task.

## Trạng thái bàn giao
- Release production: **2.1.0** tại `D:\1\cambida\release\2.1.0\CCTV_2.1.0.exe`, port 8004.
- Commit timeline-only: `5efc105`.
- Commit regression fix Cut→Replay: `338a842`.
- Production index SHA-256: `86749cd0bf9259f73a6e7c8859352403224b276cf960a4ba3dcaf32cb4e7ffe1`.
- Replay hiện chỉ dùng timeline; không nhập giờ, không Server 1/Server 2.
- Kim cố định giữa; timeline/ruler kéo bên dưới; vùng NVR có video màu xanh, gap trống; không thumbnail.
- Timeline lấy `StartTime/EndTime` NVR; playback/download dùng exact `at=` khi thả timeline.
- Cut cũng timeline-only; start/end tuyệt đối trên ngày và có thể span nhiều segment/gap; `/merge` NVR xử lý phần có dữ liệu và báo gap.
- Computer Use final PASS: Cut→Hủy→Replay giữ nguyên coverage/ruler; kéo tới 23:25:50 cho overlay và URL NVR cùng `at=2026-09-12T23:25:50`.
- Tests: py_compile PASS, JS syntax PASS, 34/34 unittest PASS.

## Config — không được tự ý thay
- Workspace config SHA-256: `ee7b820bea42c423acb05f15cd548c33706a6e0c981409ad320fec1085d0c8c3`.
- Release config SHA-256: `d0dc0fff575bdc5dc1b3068d0e45861cc92cf56c2370c872fa6fd36672161a98`.
- Task timeline chỉ deploy `release\2.1.0\index.html`; không copy/ghi đè config.

## Working tree cần lưu ý
- `.project` có cập nhật vận hành chưa commit.
- `PROJECT_SYNC_PROTOCOL.md` modified từ trước, không thuộc task timeline.
- Artifact/profile Chromium/helper cũ và `nvr_cache/` vẫn untracked; không reset/xóa hàng loạt.


## Imou direct RTSP handoff (2026-09-13 +07)
- Active source is 2.x on base `9b2a164`; do not switch back to 1.3.x for this feature.
- Current uncommitted feature files: `1.py`, `tests/test_camera_test.py`.
- Standard local Imou/Dahua generated URLs use `cam/realmonitor?channel=<n>&subtype=0|1`; ONVIF fallback appends `unicast=true&proto=Onvif`.
- Legacy/custom RTSP remains supported; existing config is not auto-rewritten.
- RAM cache stores only profile labels, not URL/credentials, and is keyed by camera connection/profile configuration.
- NVR mode always wins over stale local `*_rtsp_url` fields.
- Final gates: py_compile PASS; focused 20/20; full 46/46; diff-check PASS; synthetic assertions PASS.
- Live device verification is pending only because local camera RTSP is currently unreachable from this machine (representative check `192.168.1.206:554`).
- Backups: `backup/source_versions/`.
- No commit.


## Imou DDNS live handoff (2026-09-13 12:20 +07)
- `bidatamchin.ddns.net` is reachable and exposes TCP 554 and 37777, but 554 currently resets RTSP traffic (`-10054`) even with supplied credentials and standard Imou/Dahua paths.
- Do not diagnose this as bad password/path yet: no RTSP 401/404 is returned.
- Port 37777 is a separate Dahua/Imou private SDK/control protocol; current FFmpeg RTSP pipeline cannot substitute 37777 for 554.
- Source now recognizes remote RTSP reset and stops fallback immediately.
- Tests 47/47 PASS; config hash unchanged; credentials were not written anywhere.

## NetSDK/DVRIP 37777 handoff (2026-09-13)
- Continue in D:\1\cambida from the current dirty working tree; do not reset unrelated existing changes.
- New module: dahua_37777.py. Official Dahua x64 runtime files: vendor/dahua_netsdk/.
- 1.py now supports local_transport=netsdk for connection test, recording, live /cam and /snapshot; admin.html exposes the transport and NetSDK settings.
- tests/test_dahua_37777.py covers NetSDK adapter behavior and native DVRIP challenge login; tests/test_camera_test.py covers API/live/snapshot routing and validates no RTSP probe is used for explicit NetSDK mode.
- Current gates: 56/56 unittest PASS; py_compile PASS; admin JS syntax PASS; git diff --check PASS; config.json hash unchanged.
- Live device result: native TCP 37777 is reachable. NetSDK reports password error; raw DVRIP challenge reports 0100 authentication failed. Treat this as a credential blocker, not a transport/path failure.
- Do not brute-force, guess credentials, or persist live secrets. Once the user supplies/confirms a private-device credential accepted by 37777, rerun probe -> snapshot/live -> short MP4/ffprobe, then prepare the 3.x PyInstaller spec with vendor/dahua_netsdk DLLs and build/release.
- No commit and no 3.x artifact yet because task is BLOCKED rather than COMPLETED.

## NetSDK/DVRIP 37777 retest (2026-09-19)
- The adapter loader now accepts the resolved SDK directory; `vendor\dahua_netsdk\dhnetsdk.dll` loaded successfully in the live path.
- Retest used the existing configured credential only, with no secret output or persistence. Target was `bidatamchin.ddns.net:37777`, channel 1.
- DVRIP rejected authentication with `01000100` / `001b0002`; NetSDK rejected login with `NET_LOGIN_ERROR_PASSWORD (0x80000064, detail=1)`.
- Treat this as a private-credential blocker. No RealPlay media was received, so snapshot, live MJPEG and MP4/ffprobe gates are not claimable.
- Local gates are green: 57/57 unittest, py_compile, rendered/admin JS syntax. Config remains unchanged.
- Keep RTSP on 554 and keep 37777 as the separate private SDK transport. No 3.x release or commit until live media and ffprobe validation pass.

- [2026-09-13 13:17:24 +07:00] COMPLETED remove-license-2.1.0: rebuilt from backup/source_versions/2.1.0_pre_restore with license gate bypassed; replaced root+nested CCTV_2.1.0.exe; functional tests skipped per user; commit NONE.


## Camera transport cleanup handoff (2026-09-19)
- Task: cambida-camera-transport-clean-20260919; source workspace: D:\1\cambida.
- 1.py now normalizes camera entries by source/transport and rejects mixed payloads. admin.html renders only the fields for the selected Local NetSDK, Local RTSP, or NVR mode. Explicit NetSDK test/live/snapshot/recording paths remain separate from RTSP; replay/list follows NVR unless local backup is explicitly enabled.
- Empty state is intentional: workspace and release/embedded config camera/table arrays are zero. Backup of all four pre-clean configs is at D:\1\cambida\backup\camera-config-clean-20260919-141258; it contains the original JSON files and a metadata manifest without printed credentials.
- Separate NVR config, cctv_videos, and nvr_cache were preserved. The existing 2.1.0 binaries were not rebuilt or version-bumped because no real private NetSDK media proof exists.
- Verification: 63/63 unittest, focused transport/zero-channel 6/6, admin JS 1/1, py_compile, diff-check, and isolated authenticated source-runtime smoke on 8004 all pass. Task-scoped commit is completed after these gates.

## Final status (2026-09-19)
- Source, tests, active-config cleanup, backup, and isolated 8004 zero-channel smoke are complete.
- The task-scoped Git commit is complete; no release binary was rebuilt or version-bumped.

## 2026-09-19 — Handoff quy tắc mới

- Parent task mới: đọc `D:\1\gptagycodex.md` từ đầu tới `FAST_BOOT_END` trước tiên. Chưa đọc target/.project/source/Git hoặc gọi task tool khi chưa có xác nhận, ngoại trừ COMMA BYPASS.
- Sau xác nhận: liveness gate bắt buộc, kiểm tra workspace và `.project`/Git, không nhân đôi task. AGY/Codex CLI-only; monitor 15 phút; một final Telegram event sau verified terminal.
- `AGENTS.md` là hướng dẫn Cambida hiện hành thay thế văn bản cũ. Không tái sử dụng chính sách direct MCP, fallback tự động, timeout do im lặng hay Telegram kèm link từ bản cũ.

## Final handoff — `cambida-finalize-all-20260919`

- Source/Git finalization is complete on `main`; release line remains **2.1.0**.
- Final product scope includes `1.py`, `dahua_37777.py`, `admin.html`, timeline integration, camera/NetSDK tests, `CCTV_2.1.0.spec`, `.gitignore`, `.project` core memory and `vendor/dahua_netsdk/*.dll`.
- Gates: `python -m py_compile` PASS; full unittest **63/63 PASS**; rendered/admin Node test **1/1 PASS**; `git diff --check` PASS.
- Final distributable: `D:\1\cambida\release\2.1.0\CCTV_2.1.0.exe`; nested compatibility copy is also updated. SHA-256 for both: `E5F23EE373665FED3149569C7AB764CC7545321F0D2D1CFAF2314B5F834BC83B`.
- EXE smoke from final release returned HTTP 200 for `/`, `/admin/login`, `/timeline`; owned process cleanup passed. Config and existing media were preserved.
- Live private NetSDK media remains unverified; do not bump to 3.x without accepted credential plus snapshot/live frame and MP4/ffprobe evidence.
