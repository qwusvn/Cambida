# STATE — Cambida

Cập nhật: 2026-09-12 +07

## Trạng thái hiện tại
- Workspace: `D:\1\cambida`, branch `main`.
- Production: **2.1.0**, `D:\1\cambida\release\2.1.0\CCTV_2.1.0.exe`, port `8004`.
- Commit timeline chính: `5efc105` — `feat: switch NVR replay to timeline-only UI`.
- Commit sửa regression quay lại từ Cut: `338a842` — `fix: restore timeline after returning from cut`.
- Production index SHA-256: `86749cd0bf9259f73a6e7c8859352403224b276cf960a4ba3dcaf32cb4e7ffe1`.

## Replay/Timeline hiện tại
- Timeline-only: không còn ô nhập giờ, không còn mode chọn giờ cũ.
- Không có Server 1/Server 2; dùng duy nhất NVR đã cấu hình cho camera.
- Kim/playhead cố định giữa; kéo ruler/timeline chạy bên dưới.
- Đoạn có bản ghi NVR tô xanh; gap để trống; không thumbnail.
- Timeline dùng `StartTime/EndTime` NVR làm source of truth.
- Khi thả kéo, playback/download NVR dùng `at=YYYY-MM-DDTHH:MM:SS` đúng mốc dưới kim.
- Cut cũng timeline-only; start/end là datetime tuyệt đối trên cả ngày, có thể đi qua nhiều segment/gap, tối đa theo `MAX_MERGE_MINUTES`.
- Khi Hủy Cut quay lại replay, timeline coverage/ruler được render lại sau khi màn hình hiện, tránh mất vùng xanh/cắt nhãn.

## Kiểm chứng
- `python -m py_compile 1.py`: PASS.
- `python -m unittest discover -s tests`: **34/34 PASS**.
- Rendered JavaScript `node --check`: PASS.
- `git diff --check`: PASS trước commit.
- GUI Computer Use final: PASS — không time input/source selector; fixed center playhead; green coverage/gaps; Cut → Hủy → replay vẫn đầy đủ; kéo tới `23:25:50` cho overlay và NVR URL cùng `at=2026-09-12T23:25:50`.
- Cross-segment test Cam 2 `22:28:55→22:30:25`: backend phát hiện gap 76 giây, ghép phần có dữ liệu thành MP4 H.264 1080p duration 13.21s; file test đã xóa.
- Production sau deploy: `/` = 200, `/replay/cam2` = 200, port 8004 listening.

## Bảo vệ config
- `D:\1\cambida\config.json` SHA-256 giữ nguyên: `ee7b820bea42c423acb05f15cd548c33706a6e0c981409ad320fec1085d0c8c3`.
- `D:\1\cambida\release\2.1.0\config.json` SHA-256 giữ nguyên: `d0dc0fff575bdc5dc1b3068d0e45861cc92cf56c2370c872fa6fd36672161a98`.
- Deploy task này chỉ thay `release\2.1.0\index.html`; không copy/ghi lại config.

## Ngoài scope còn tồn tại
- `.project/*` là memory vận hành, chưa commit.
- `PROJECT_SYNC_PROTOCOL.md` modified từ trước, không thuộc task này.
- Các artifact/profile Chromium/helper cũ và `nvr_cache/` vẫn giữ nguyên, không xóa hàng loạt.

## RTSP local timeline - BLOCKED (2026-09-13 08:46:17 +07:00)
- Runtime production 2.1.0 port 8004 v?n ho?t d?ng; replay/timeline d� �p local RTSP, kh�ng d�ng NVR playback.
- �� ph�t hi?n v� d?ng 97 ffmpeg.exe m? c�i ch? trong D:\1\cambida; production t? t?o l?i 3 worker FFmpeg c� parent h?p l? PID 2484.
- Sau cleanup, Cam 1-3 production v?n timeout RTSP; test_camera_connection c? 3 th?t b?i do ph?n h?i qu� ch?m.
- Camera local 192.168.1.206/209/210 hi?n kh�ng ping v� TCP 554 kh�ng k?t n?i; Yato Ethernet 192.168.1.220 v� gateway 192.168.1.1 v?n ping OK.
- DDNS NVR v?n resolve v� TCP 81/554 m? nhung RTSP/HTTP application kh�ng tr? media k?p; blocker hi?n l� ngu?n/network camera/NVR, kh�ng ph?i timeline UI/backend.
- Git source hi?n c� commit 2d30fcd + 9b2a164; task chua terminal v� chua c� MP4 RTSP production m?i d? x�c nh?n end-to-end.


## Imou direct RTSP ? implementation complete / live validation blocked (2026-09-13 +07)
- Active development baseline: Git `9b2a164` (2.x local RTSP timeline branch).
- Implemented direct local Imou/Dahua RTSP profiles without modifying `config.json`.
- Candidate order supports Dahua/Imou `cam/realmonitor` main/sub, ONVIF variant, configured/custom path, and legacy fallback; successful profile is cached only in RAM as a non-secret label.
- Recording uses real FFmpeg attempts for fallback (no per-segment preflight); preview follows the cached working profile.
- NVR path remains isolated and one-URL; stale local direct overrides cannot bypass NVR mode.
- RTSP stderr is sanitized before CAM_LAST_ERROR/log/Telegram to avoid credential leakage.
- Tests: `py_compile` PASS, focused Imou tests 20/20 PASS, full unittest 46/46 PASS, `git diff --check` PASS, synthetic RTSP assertions PASS.
- `config.json` unchanged SHA-256: `EE7B820BEA42C423ACB05F15CD548C33706A6E0C981409AD320FEC1085D0C8C3`.
- Live E2E blocker: configured local camera `192.168.1.206:554` is currently TCP unreachable from this host; earlier local camera IPs 206/209-214 also had no reachable RTSP/HTTP/Dahua ports during probe.
- Source backups before this work are under `backup/source_versions/` including `1.3.42_clean`, `1.3.5_wip`, `2.1.0_pre_restore`, `1.3.4_reference`, and `2.1.0_imou_codex_pre_review`.
- No commit created.


## Imou public DDNS live probe (2026-09-13 12:20 +07)
- Target: `bidatamchin.ddns.net` resolved to `27.70.239.198`. User supplied credentials were used only in live probe and are NOT stored in `.project`, source or config.
- TCP observed OPEN: 80, 81, 443, 554, 37777.
- RTSP 554 with authenticated Imou/Dahua main, sub and ONVIF variants all failed with Windows socket reset (`Error number -10054`) before any 401/404/media response.
- Raw RTSP OPTIONS and HTTP request to 554 are reset immediately; TLS handshake on 554 fails, so the exposed 554 endpoint is not behaving as a usable plain RTSP or simple RTSPS service from this host.
- 37777 accepts TCP but does not speak RTSP/HTTP, consistent with a proprietary Dahua/Imou SDK/control port rather than RTSP media.
- Current blocker is therefore public RTSP service/port-forward/NAT/device RTSP state, not RTSP path selection or proven credential rejection.
- Backend now classifies WinSock reset `-10054` / WSAECONNRESET as a dedicated RTSP reset condition and short-circuits pointless path fallback.
- Verification after fix: focused camera tests 21/21 PASS; full suite 47/47 PASS; `py_compile` PASS; `git diff --check` PASS.
- `config.json` unchanged SHA-256 `EE7B820BEA42C423ACB05F15CD548C33706A6E0C981409AD320FEC1085D0C8C3`.

## NetSDK/DVRIP 37777 adapter - BLOCKED (2026-09-13 12:47:43 +07:00)
- task_id: chatgpt-cambida-netsdk37777-20260913
- executor: ChatGPT
- ownership: ChatGPT
- workspace: D:\1\cambida
- status: BLOCKED
- scope: separate Dahua/Imou private TCP 37777 adapter; direct media test, recording, live preview and snapshot without RTSP fallback.
- progress: official Dahua x64 NetSDK is under vendor/dahua_netsdk; new dahua_37777.py supports high-level NetSDK login, RealPlay, SaveRealData -> MP4, RealData callback -> FFmpeg DHAV -> MJPEG, and normal DVRIP realm/challenge auth diagnostics.
- integration: local_transport=netsdk is wired into admin config, /api/admin/test-camera, recorder, /cam live MJPEG and /snapshot; existing RTSP/NVR paths remain isolated.
- verification: full unittest 56/56 PASS; py_compile PASS; admin JS syntax PASS; git diff --check PASS. config.json SHA-256 remains EE7B820BEA42C423ACB05F15CD548C33706A6E0C981409AD320FEC1085D0C8C3.
- live blocker: target TCP 37777 answers native Dahua protocol, but current configured credential is rejected by both NetSDK (0x80000064 password error) and normal DVRIP challenge login (0100 authentication failed). No credential/hash was written to project memory/source/tests.
- release: do NOT bump to 3.x or build a 3.x release until a real 37777 session receives media and a recorded MP4/live frame is validated end-to-end.
- updated_at: 2026-09-13 12:47:43 +07:00

## Correction 37777 (2026-09-13 12:54:52 +07:00)
- Previous live result used incorrect letter case and is invalid.
- Status: pending live retest with corrected input; do not classify device-side rejection.
- Do not release 3.x until live NetSDK media succeeds end-to-end.


## 2026-09-19 - Camera transport cleanup (cambida-camera-transport-clean-20260919)
- workspace: D:\1\cambida; executor: ChatGPT; status: COMPLETED.
- Camera configuration is canonical and transport-disjoint: Local NetSDK, Local RTSP, and NVR no longer retain each other's fields; explicit NetSDK paths do not fall back to RTSP; replay/list/recording source selection follows the camera source.
- Authorized cleanup backed up and cleared camera/table entries in the workspace config plus the three release/embedded config copies. Global settings, storage paths, existing videos, and the separate nvr\nvr_config.json were preserved. Backup: D:\1\cambida\backup\camera-config-clean-20260919-141258.
- No Cambida process/listener owned port 8004 before the operation. An isolated source-runtime smoke served 8004 and verified authenticated admin, zero cameras/tables, /live, and /timeline; the smoke process was then closed, so no unrelated service was restarted or killed.
- Gates: full unittest 63/63 PASS; transport/zero-channel focused 6/6 PASS; admin rendered JavaScript 1/1 PASS; py_compile PASS; diff-check PASS.
- Real device media remains unverified because no valid private credential was available; no 3.x release/version bump.

## 2026-09-19 - Final status
- status: COMPLETED. Implementation, config cleanup, tests, and isolated 8004 runtime verification are complete.
- The task-scoped Git commit was created after all acceptance gates passed; no unrelated changes were staged, and no release binary was rebuilt or version-bumped.

## 2026-09-19 — Đồng bộ quy tắc điều phối

- Task: cambida-global-rules-sync-20260919; executor: ChatGPT + Remote Desktop Commander/Yato; scope: tài liệu điều phối, không sửa source/config/release/runtime.
- Nguồn quy tắc hiệu lực: `D:\1\gptagycodex.md` FAST BOOT; `AGENTS.md` đã thay phần quy tắc cũ bằng tham chiếu và ràng buộc dự án.
- Working tree đã có thay đổi source và `.project` từ trước; không được coi các thay đổi đó là kết quả của task đồng bộ quy tắc, không stage/commit lẫn.
- Verified: AGENTS.md được commit riêng `f7a5246`; `git diff --check` không báo lỗi; các thay đổi cũ trong `.project`/source không bị stage hoặc commit.
- Status: COMPLETED (đồng bộ hướng dẫn; không có thay đổi runtime hay mã nguồn).

## 2026-09-19 - Finalize all completed camera/Imou/NetSDK work

- Parent task: cambida-finalize-all-20260919; status: COMPLETED.
- Integrated source includes canonical transport cleanup, Imou/Dahua RTSP handling, separate NetSDK/DVRIP 37777 adapter, admin UI, tests and the official x64 vendor DLL set.
- Version remains 2.1.0. No 3.x bump: accepted private credential, live snapshot/frame and MP4/ffprobe evidence are still unavailable.
- Verification: python -m py_compile PASS; full python -m unittest discover -s tests 63/63 PASS; rendered/admin Node test 1/1 PASS; git diff --check PASS.
- Packaging: PyInstaller 6.16.0 onedir PASS from CCTV_2.1.0.spec; 10 NetSDK DLLs present in each final onedir bundle; source EXE smoke and final release EXE smoke returned /, /admin/login, /timeline = HTTP 200 with owned process cleanup PASS.
- Final artifacts: D:\1\cambida\release\2.1.0\CCTV_2.1.0.exe and nested compatibility copy; both SHA-256 E5F23EE373665FED3149569C7AB764CC7545321F0D2D1CFAF2314B5F834BC83B.
- Release root/nested config hashes and the five existing cctv_videos media files were preserved during deployment. Generated build/ and dist/ were removed after packaging; no camera credential was persisted or printed.
## 2026-09-20 — Replay timeline/overlay fix
- Task `cambida-timeline-overlay-fix-20260920` status: COMPLETED.
- Timeline coverage chỉ gộp phần hiển thị khi khoảng cách giữa hai segment <= 2000 ms; dữ liệu video/backend không bị thay đổi và gap lớn vẫn hiện đúng.
- Custom player controls mặc định ẩn; pointer/touch/keyboard/focus làm hiện controls và timer 2400 ms tự ẩn khi idle. Fullscreen/accessibility/fixed-center playhead/exact-time flow được giữ nguyên.
- Source `index.html` và ignored runtime sidecar `release\2.1.0\index.html` đã được cập nhật; `config.json` và `RELEASE_VERSION.txt` không đổi.
- Verification: Node focused 4/4 PASS; full Python unittest 78/78 PASS; py_compile PASS; inline JS compile source+release PASS; git diff --check PASS.
- Commit: `bb268a93527b60bfeee4b592a94d3ea69d9ec313`; commit scope chỉ gồm `index.html` và `tests/replay_timeline_ui.test.js`.

## 2026-09-20 — Replay controls refinement
- Task `cambida-replay-controls-20260920`: live chuyển ngay trong replay và timeline vẫn hiển thị, tự nhảy về ngày/thời điểm hiện tại.
- Zoom -/+ đã chuyển lên toolbar nhưng giữ nguyên chức năng.
- Hai nút 2x/4x rời được thay bằng một nút cho mỗi hướng, mặc định 1x và quay vòng 1x -> 2x -> 4x -> 1x khi tiếp tục bấm cùng hướng.
- Source `index.html`, regression `tests/replay_timeline_ui.test.js`, và ignored sidecar `release\2.1.0\index.html` đã đồng bộ.
- Gates: Node 5/5, Python 78/78, py_compile, diff-check đều PASS. Config/version/transport không thay đổi.
