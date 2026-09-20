# TASKS — Cambida

## DONE
- [x] Khôi phục replay UI từ HTML thật 1.3.42, giữ backend NVR hiện tại.
- [x] Dùng một nguồn NVR đã cấu hình; bỏ Server 1/Server 2.
- [x] Gỡ license gate khỏi replay/timeline/cut/download.
- [x] Sửa NVR exact time + `/merge` cắt trực tiếp NVR (`f16d752`).
- [x] 2026-09-12 — Chuyển replay sang **timeline-only**: bỏ nhập giờ; playhead cố định giữa; kéo timeline bên dưới; coverage xanh, gap trống, không thumbnail (`5efc105`).
- [x] 2026-09-12 — Cut timeline dùng mốc datetime tuyệt đối trên toàn ngày, hỗ trợ khoảng qua nhiều segment/gap.
- [x] 2026-09-12 — Sửa Cut → Hủy → replay bị mất coverage/ruler (`338a842`).
- [x] GUI final PASS; overlay và NVR `at=` khớp tại `23:25:50`.
- [x] Cross-segment NVR cut test PASS; gap 76s được báo đúng, output phần có dữ liệu 13.21s.
- [x] py_compile PASS, JS syntax PASS, **34/34 unittest PASS**.
- [x] Deploy `release\2.1.0\index.html` và restart production 8004; config workspace/release giữ nguyên SHA-256.

## DOING
- Không có task thuộc batch hiện tại.

## TODO
- [ ] Theo dõi trải nghiệm timeline thực tế trên máy khách/Cốc Cốc với các ngày có nhiều gap và các ca cắt dài.
- [ ] Chỉ dọn artifact/profile cũ khi xác định rõ là file tạm.

## BLOCKED
- Không có blocker.


## Imou direct RTSP (2026-09-13 +07)
- [x] Backup source 1.3.42 clean, 1.3.5 WIP, 2.x pre-restore, 1.3.4 reference before continuing development.
- [x] Restore active 2.x baseline `9b2a164` byte-for-byte before Imou work.
- [x] Add Imou/Dahua direct RTSP candidate + ONVIF + legacy/custom fallback without rewriting config.
- [x] Add RAM-only working-profile cache shared by recording/preview.
- [x] Preserve NVR behavior and prevent stale local direct URL from overriding NVR mode.
- [x] Redact RTSP credentials from recording errors/log/Telegram.
- [x] Add focused tests and full regression verification: 46/46 PASS.
- [ ] Live E2E Imou verification once camera RTSP TCP 554 is reachable from the Cambida host.


## Imou DDNS live validation (2026-09-13 12:20 +07)
- [x] Resolve DDNS and verify exposed ports.
- [x] Probe authenticated main/sub/ONVIF RTSP without persisting credentials.
- [x] Distinguish authentication/path failure from transport reset.
- [x] Add regression handling for Windows RTSP reset `-10054`; full suite 47/47 PASS.
- [ ] Fix/verify external port 554 forwarding or camera RTSP service, then rerun live E2E media probe.
- [ ] If direct video must use port 37777 instead of RTSP 554, evaluate a separate Dahua NetSDK/DVRIP adapter; do not treat 37777 as an RTSP port.

## Dahua/Imou private 37777 adapter (2026-09-13)
- [x] Add separate x64 Dahua NetSDK backend; do not treat 37777 as RTSP.
- [x] Add normal DVRIP realm/random challenge authentication diagnostic.
- [x] Add private 37777 media probe requiring actual RealPlay data.
- [x] Add recorder path: NetSDK RealPlay/SaveRealData -> DHAV -> FFmpeg MP4.
- [x] Add direct 37777 live MJPEG and snapshot paths via RealData callback + FFmpeg DHAV demuxer.
- [x] Add admin local_transport selector and NetSDK port/channel/main-sub fields.
- [x] Preserve RTSP/NVR behavior and prevent automatic RTSP fallback for NetSDK-selected cameras.
- [x] Verify source: 56/56 unittest PASS, py_compile PASS, admin JS syntax PASS, diff-check PASS, config hash unchanged.
- [ ] BLOCKED: obtain/confirm the device credential accepted by native TCP 37777; current configured credential returns NetSDK password error and DVRIP 0100 auth failure.
- [ ] After valid auth: live media probe, snapshot, live MJPEG, 5-10 second MP4 + ffprobe; only then package/bump to 3.x.

## Dahua/Imou private 37777 retest (2026-09-19)
- [x] Fix the resolved NetSDK directory handoff so the official DLL is loaded by the integrated adapter.
- [x] Retest the existing configured credential without persisting or exposing it; TCP/native protocol and NetSDK both reach authentication.
- [x] Record precise rejection evidence: DVRIP `01000100` / `001b0002`, NetSDK `0x80000064` / `detail=1`.
- [x] Run local gates: py_compile PASS, full unittest 57/57 PASS, rendered/admin JS syntax PASS.
- [ ] BLOCKED: private credential is rejected before media; snapshot/live frame/MP4/ffprobe remain unverified. Do not bump to 3.x.

- [2026-09-13 13:17:24 +07:00] COMPLETED remove-license-2.1.0: rebuilt from backup/source_versions/2.1.0_pre_restore with license gate bypassed; replaced root+nested CCTV_2.1.0.exe; functional tests skipped per user; commit NONE.


## Camera transport cleanup (2026-09-19)
- task_id: cambida-camera-transport-clean-20260919
- [x] Canonicalize and validate Local NetSDK, Local RTSP, and NVR payloads with no stale cross-transport fields or implicit RTSP fallback.
- [x] Route test/live/snapshot/recording/replay/list by the selected camera source; preserve explicit RTSP and isolate NVR.
- [x] Add mode-switch, add/remove, zero-camera, reload, and rendered-admin regressions.
- [x] Back up and clear all active workspace/release camera lists and legacy per-camera entries without changing global settings or video data.
- [x] Verify source runtime over port 8004 with zero-channel admin/API state.
- [x] Create the task-scoped Git commit; live device snapshot/MP4/ffprobe remains unverified and 3.x remains gated.

## Camera transport cleanup final status
- [x] Implementation and verification complete.
- Status: COMPLETED after the task-scoped Git commit.

## Đồng bộ quy tắc toàn cục — 2026-09-19
- task_id: cambida-global-rules-sync-20260919
- [x] Đọc FAST BOOT trong global spec và xác định workspace Cambida.
- [x] Kiểm tra kết nối Yato, `.project`, Git và quy tắc cũ trong AGENTS.md.
- [x] Thay AGENTS.md cũ bằng quy tắc dự án tham chiếu nguồn toàn cục duy nhất.
- [x] Ghi nhận quyết định và quy tắc mới trong `.project`.
- [ ] Kiểm tra diff đúng phạm vi, tạo commit task và xác minh kết quả.
- [x] Kiểm tra `git diff --check` không có lỗi; commit riêng AGENTS.md: `f7a5246`.
- Status: COMPLETED đối với cập nhật tài liệu điều phối. Các tệp `.project` vốn đã dirty được giữ nguyên trạng thái, không commit gộp thay đổi cũ.

## Finalize all completed camera/Imou/NetSDK work — 2026-09-19

- task_id: `cambida-finalize-all-20260919`
- [x] Kiểm kê dirty worktree và phân loại source/product với probe, preview, cache, runtime và release junk.
- [x] Tích hợp source, tests, `.project` memory, PyInstaller spec và 10 vendor DLL NetSDK cần cho runtime.
- [x] Giữ release line 2.1.0; không bump 3.x khi live NetSDK media proof còn thiếu.
- [x] `py_compile` PASS; full unittest **63/63 PASS**; rendered/admin JS **1/1 PASS**; `git diff --check` PASS.
- [x] PyInstaller 6.16.0 onedir build PASS; source and final release EXE smoke PASS (`/`, `/admin/login`, `/timeline` đều 200); process/temp cleanup PASS.
- [x] Bảo toàn config và năm media files hiện có; artifact SHA-256: `E5F23EE373665FED3149569C7AB764CC7545321F0D2D1CFAF2314B5F834BC83B`.
- [x] Stage/review/commit và hậu kiểm working tree thuộc parent task.
- Status: COMPLETED.

## Replay timeline + idle overlay fix — 2026-09-20
- task_id: cambida-timeline-overlay-fix-20260920
- [x] Gộp hiển thị các đoạn ghi hình sát nhau khi khoảng lệch phân đoạn <= 2 giây; khoảng trống thật vẫn giữ nguyên.
- [x] Ẩn cụm điều khiển video khi không thao tác; hiện lại khi tương tác/focus và tự ẩn sau 2,4 giây.
- [x] Đồng bộ thay đổi vào source `index.html` và sidecar `release\2.1.0\index.html` mà không thay config/version/camera transport.
- [x] Regression: Node 4/4 PASS; Python unittest 78/78 PASS; py_compile PASS; inline JS source+release PASS; git diff --check PASS.
- [x] Task-scoped commit: `bb268a93527b60bfeee4b592a94d3ea69d9ec313` (chỉ `index.html` + `tests/replay_timeline_ui.test.js`).
- Status: COMPLETED.

## Replay live / zoom / playback controls — 2026-09-20
- task_id: cambida-replay-controls-20260920
- [x] Nút Xem trực tiếp chuyển player sang live ngay trong replay, giữ timeline hiển thị và đưa ngày/kim timeline tới thời điểm hiện tại.
- [x] Chuyển cụm zoom -/+ lên hàng toolbar; giữ nguyên logic zoom timeline.
- [x] Gộp mỗi hướng phát thành một nút: << 1x và 1x >>; khi cùng hướng đang chạy, mỗi lần bấm quay vòng 1x -> 2x -> 4x -> 1x.
- [x] Áp dụng đồng nhất cho replay và cut; giữ fixed-center playhead, coverage/gap, cut flow và idle controls.
- [x] Đồng bộ sidecar release\2.1.0\index.html với source; không đổi config/version/camera transport.
- [x] Verification: replay Node 5/5 PASS; full Python unittest 78/78 PASS; py_compile PASS; git diff --check PASS.
- Status: COMPLETED sau task-scoped commit.

## Replay seek/live guard + packaged launcher — 2026-09-20
- task_id: `cambida-replay-launcher-20260920`
- [x] Scrub/drag/keyboard seek tự chuyển recorded playback và autoplay đúng datetime; giữ fixed-center needle, coverage/gap, Cut và speed/zoom.
- [x] Player request serial loại callback `loadedmetadata` cũ; live list refresh/programmatic now jump không được load replay; user scrub khi live mới thoát live và phát replay.
- [x] `Xem trực tiếp` giữ stream `/cam...` và không gọi recorded playback; regression Node **9/9 PASS**.
- [x] Launcher tracked mới: `D:\1\cambida\CCTV_2.1.0.launcher.cmd`; reuse port 8004, chỉ start `release\2.1.0\CCTV_2.1.0.exe` khi cần, rồi mở browser.
- [x] Full Python unittest **81/81 PASS**, launcher **3/3 PASS**, py_compile và `git diff --check` PASS.
- [ ] Rebuild EXE chưa áp dụng: PyInstaller bị `WinError 5` khi đọc `C:\Users\qwusv\AppData\Roaming\Python\Python313\site-packages`; runtime/user server không bị restart.
- [ ] Codex sandbox không tạo được Git commit: `.git\index` từ chối tạo `index.lock` với `Permission denied` do ACL read-only của execution token.
- Status: IMPLEMENTATION VERIFIED; packaged EXE rebuild BLOCKED by host permission.
