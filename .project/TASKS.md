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
- [x] 2026-09-21 — Tối ưu độ trễ luồng trực tiếp NetSDK từ 8.5s xuống 0.8s, triệt tiêu đệm trôi lag; giữ nguyên 100% luồng ghi hình 60s (`0b7a585`).
- [x] 2026-09-21 — Thêm biểu tượng loading spinner khi chờ nạp luồng trực tiếp (`00b7421`).
- [x] 2026-09-22 — Cải tiến UI player: icon xem trực tiếp sóng radio cân đối; nhãn "Tốc độ phát" & "Thu phóng"; cụm nút tốc độ đặt sẵn 0.5X/1X/2X/4X với gạch chân xanh nút active; timeline cắt video hiển thị nhãn "(Hôm trước)" và render coverage/ruler đầy đủ.
- [x] 2026-09-22 — Đóng gói phát hành bản 2.1.1: tạo spec và build PyInstaller onedir CCTV_2.1.1.exe (482 KB, bundle 10 DLL NetSDK); triển khai release\2.1.1\ đầy đủ sidecar HTML, ffmpeg.exe, config.json.
- [x] 2026-09-22 — Loại bỏ ràng buộc ổ D: và tối ưu chạy đa ổ đĩa cho 2.1.1: import an toàn winreg trong 1.py; chuyển INSTANCE_STATE_FILE sang thư mục temp chuẩn của OS; chuyển CCTV_2.1.1.spec sang đường dẫn động qua SPECPATH; cập nhật CCTV_2.1.1.launcher.cmd và bổ sung Chay_CCTV.cmd trong release\2.1.1 để chạy độc lập mọi ổ đĩa (C:, D:, USB). Rebuild và đóng gói lại bản 2.1.1 hoàn tất.
- [x] 2026-09-22 — Đảo chiều mô hình lưu trữ: Local là chính, NVR là dự phòng. Camera NVR luôn ghi hình cục bộ trên PC; giao diện replay/timeline mặc định tải Local, hỗ trợ nút toggle chuyển sang NVR dự phòng; cắt video ưu tiên Local và tự động bù NVR khi khuyết dữ liệu.
- [x] 2026-09-22 — Tích hợp GitHub Auto-Updater (qwusvn/Cambida) + Báo Telegram: tự động kiểm tra GitHub Releases mỗi 60 phút hoặc qua lệnh Telegram /update, tải ngầm file zip, bàn giao cho updater.cmd thay thế file bảo toàn 100% config/db/videos, khởi động lại và gửi thông báo thành công qua Telegram.
- [x] 2026-09-22 — Bộ kiểm thử tự động 99/99 unittest PASS (bao gồm test_auto_update.py mới, test_nvr_per_camera.py cập nhật).
- [x] 2026-09-23 — Đưa toàn bộ mã nguồn lên GitHub: liên kết origin https://github.com/qwusvn/Cambida.git, commit và hợp nhất lịch sử sạch sẽ, bảo vệ an toàn các file cấu hình và dữ liệu nhạy cảm, push thành công lên origin/main (HEAD: 7437fad).

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

## Replay timeline autoplay repair — 2026-09-20
- task_id: `cambida-timeline-autoplay-20260920`
- [x] Xác định page đang phục vụ trên PID 3000 là template cũ: `/replay/cam1` 64628 bytes, thiếu `playerRequestSerial` và `userInitiated`; source/sidecar mới 65902 bytes.
- [x] Sửa autoplay seek cho pointer/touch release, keyboard và các nhánh đổi segment/NVR: giữ exact recorded datetime, request serial, retry `play()` qua metadata/canplay, dọn waiter stale; live button vẫn chỉ chuyển live/needle.
- [x] Đồng bộ `release\2.1.0\index.html` với source; không sửa config, port, video hoặc EXE.
- [x] Regression behavior mới: Node **12/12 PASS**; full Python unittest **81/81 PASS**; `py_compile` và `git diff --check` PASS.
- [x] Commit task-scoped: `70eb752` (`index.html`, `tests/replay_timeline_ui.test.js`).
- [ ] Orchestrator restart PID 3000 rồi chạy lại HTTP marker check và Chrome/CDP local-file playback smoke; trước restart runtime vẫn là template cũ và player vẫn paused sau seek.
- Status: SOURCE FIX COMMITTED; RUNTIME RESTART/POST-RESTART SMOKE PENDING ORCHESTRATOR.

- [x] Orchestrator restarted the verified source server (old PID 3000 stopped, new PID 13764 listening on 8004); post-restart HTTP /, /timeline, /replay/cam1 returned 200 and replay page contains playerRequestSerial + userInitiated + canplay.
- [ ] Post-restart real-browser media playback smoke not performed: remote command to launch standalone smoke was blocked by platform safety checks. Node behavioral regression 12/12 and Python 81/81 passed prior to restart.
- Status: SOURCE FIX COMMITTED AND SOURCE SERVER RESTARTED; HTTP DEPLOY VERIFIED, REAL BROWSER PLAYBACK NOT VERIFIED.

## Live sub preview and no video overlay — 2026-09-20
- task_id: `cambida-live-sub-no-overlay-20260920`
- [x] Live preview route uses a transient `sub` stream for replay/cut and all-camera live views; configured `view_stream`, recording Main, replay quality, camera config and NVR transport remain unchanged.
- [x] Removed timestamp/title and floating player controls from the video frame; pause, time, fullscreen and timeline controls remain accessible outside the video.
- [x] Added backend, replay/cut, all-camera live and overlay regressions; synced ignored `release\2.1.0\index.html` to source.
- [x] Full verification: Node **14/14**, Python unittest **83/83**, `py_compile`, inline JS source/release, `node --check`, and `git diff --check` PASS.
- [x] Task-scoped commit `5f607fa` contains only source/tests; server restart remains with the orchestrator.
- Status: COMPLETED — source verified; no EXE rebuild or in-turn server restart.

## 2026-09-20 — Live sub/no-overlay deployment verification
- [x] Orchestrator restarted ONLY verified Cambida 1.py server PID 13764 on port 8004; new Python server PID 4508 now owns port 8004.
- [x] HTTP GET `/`, `/live`, `/timeline`, `/replay/cam1` all returned 200 after restart; live/replay HTML requests `stream=sub&view=live_preview`; replay frame contains no player-top/player-bottom/player-controls elements and external player-toolbar exists.
- [x] Workspace and release config SHA-256 unchanged: BE20491EB947C439A1C94DB4411ED18E1A6A4E224386702A8A1F08882F26D4E4 / 89136696CF59481E7CB88C1D4C0F4DC9A679EC9264FAA499AB6B294813ABCE88. Source and release HTML hashes match.
- [ ] Real-camera frame/latency and browser GUI playback after restart not measured; HTTP and unit tests do not prove actual latency.
- Parent status: COMPLETED for source/deployment acceptance; actual camera latency remains unverified.

## Four approved Cambida playback/startup/timeline changes — 2026-09-21
- task_id: `cambida-4-approved-changes-20260921`
- [x] (1) Replay/cut auto-starts real live sub preview on initial load (`setScreenLive("replay", true)` in DOMContentLoaded) rather than a static snapshot image, while preserving timeline seeking/scrubbing to switch to recorded autoplay.
- [x] (2) Server/app startup does not automatically open browser on launch (removed `_open_server_page` on startup and in launcher loop); double-clicking existing system tray icon (pystray `default=True` "Mở Cambida") opens/focuses the web GUI. Mutex single-instance protection preserves running server without duplicate spawns.
- [x] (3) Timeline defaults to one full day view (initial zoom = 1); labels hours continuously across midnight (0h..24h); day navigation (`changeTimelineDay`) and exact timestamp day rollover for recorded playback and cut/merge.
- [x] (4) Visibly distinct boundary divider (`.filmstrip-boundary`) rendered between adjacent recorded video segments even if no gap / within jitter; user-adjustable segment boundary color configured and persisted via `site.theme.segment_boundary` in admin settings, `config.json`, and CSS variable `--segment-boundary`.
- [x] Synced ignored `release\2.1.0\index.html` identically to `index.html`.
- [x] Regressions verified: Python unittests **94/94 PASS**; Node tests **19/19 PASS** (15 replay timeline, 3 admin logic, 1 live preview); `py_compile 1.py` PASS; `git diff --check` PASS.
- [x] Task-scoped commit `b6fe156` created for the 8 modified source/test files.
- Status: COMPLETED.



## Khôi phục mốc 042b9d8 — 2026-09-21
- task_id: `cambida-restore-042b9d8-20260921`; status: COMPLETED cho source rollback, build, release deploy và HTTP/recording smoke.
- [x] Backup source/config/data/runtime cũ vào `backup/rollback-to-042b9d8-20260921-013230`.
- [x] Khôi phục 8 tracked source/test paths đúng commit `042b9d8`, giữ nguyên `tests/test_stream_copy_merge.py`; scoped rollback commit `a9d4120`.
- [x] Python unittest 92/92; Node UI 14/14; source diff so với target sạch.
- [x] Build/deploy lại hai EXE chính thức của release 2.1.0; bảo toàn workspace/root/nested config và media, đồng bộ source/release HTML.
- [x] Source runtime PID 32940 phục vụ cổng 8004, bắn route HTTP 200; MP4 mới sau restart giải mã FFmpeg thành công.
- [ ] GUI playback trực tiếp và đóng gói EXE chạy độc lập chưa được kiểm thử; có cảnh báo Windows autostart `winreg` chưa định nghĩa tồn tại tại mốc 042b9d8; không tự sửa ngoài phạm vi rollback.

## Timeline previous day display and day rollover - 2026-09-21
- task_id: `cambida-timeline-prevday-20260921`
- [x] (1) Timeline displays previous day mark (`Hôm trước`) at hour 0 instead of `0h` / `00:00`, with click action navigating directly to the previous day (`changeTimelineDay(-1)`).
- [x] (2) Timeline dragging/scrubbing to the left past 0h smoothly moves into and rolls over to the previous day (updating date picker, loading videos, and positioning playhead at corresponding time).
- [x] (3) Date navigation buttons (`previousDayButton`, `nextDayButton`, `cutPreviousDayButton`, `cutNextDayButton`) integrated next to date picker.
- [x] (4) Timeline default zoom set to 1 (full day view); hourly ruler labels rendered (`Hôm trước`, `1h`, `2h`, ..., `24h`).
- [x] (5) Synced ignored runtime sidecar `release\2.1.0\index.html` byte-identically to source `index.html` (SHA-256 matches).
- [x] Regressions verified: Node tests **18/18 PASS** (`tests/replay_timeline_ui.test.js`, `tests/admin_camera_logic.test.js`, `tests/live_preview_ui.test.js`); Python unittests **92/92 PASS**; `py_compile 1.py` PASS; `git diff --check` PASS.
- [x] Task-scoped commit `b28a546` created for `index.html` and `tests/replay_timeline_ui.test.js`.
- Status: COMPLETED.

## Replay UI/timeline streamlining & continuous 48h ruler - 2026-09-21
- task_id: cambida-replay-streamline-20260921
- [x] (1) Removed play button and time readout toolbar under video; removed timeline zoom in/out buttons; converted fullscreen button into an overlay button (.video-overlay-button) inside #playerFrame and #cutPlayerFrame.
- [x] (2) Removed prefix "Hôm nay, " from date picker label (showing date only) and removed "Hôm trước" text from timeline ruler.
- [x] (3) Positioned forward speed button (1x >>) directly adjacent to reverse speed button (<< 1x) in .timeline-rate-group on the left of timeline toolbar.
- [x] (4) Fixed speed cycling click handler to immediately apply next speed on first click (cycles 2x -> 4x -> 1x -> 2x).
- [x] (5) Seamless continuous 48h timeline ruler (... 22h 23h 24h 0h 1h 2h ...) spanning yesterday and today, loading recordings across both days continuously.
- [x] (6) Removed "Tải đoạn này về máy" button; renamed "Bắt đầu cắt video" to "Cắt Video" styled with primary blue gradient button visual.
- [x] (7) Live preview plays directly in-page on current player, not opening a new page or tab.
- [x] (8) Initial page load defaults to live preview (setScreenLive("replay", true) in DOMContentLoaded), switching to replay on timeline scrub.
- [x] Synced ignored runtime sidecar
elease\2.1.0\index.html byte-identically with source index.html.
- [x] Regressions verified: Node tests **18/18 PASS**, Python unittests **92/92 PASS**, py_compile 1.py PASS, git diff --check PASS.
- [x] Task-scoped commit for index.html and 	ests/replay_timeline_ui.test.js.
- Status: COMPLETED.

## Replay zoom controls, 5h default zoom, admin restart & silent startup - 2026-09-21
- task_id: cambida-zoom-admin-restart-silent-20260921
- [x] (1) Restored timeline zoom buttons (#timelineZoomOut, #timelineZoomIn, #cutTimelineZoomOut, #cutTimelineZoomIn) to replay and cut toolbars.
- [x] (2) Set default timeline zoom to display ~5 hours visible in the viewport (DEFAULT_TIMELINE_ZOOM = 48 / 5 = 9.6).
- [x] (3) Added restart server buttons to admin.html (safeRestart in Operation Center and restartServerSticky in sticky-save bar) wired to POST /api/admin/restart with automatic status polling.
- [x] (4) Added restart_server() background thread and POST /api/admin/restart in 1.py; removed automatic browser launch (_open_server_page) on server startup; added 'Mở Cambida' default MenuItem in pystray tray.
- [x] Synced ignored runtime sidecar release\2.1.0\index.html byte-identically with source index.html.
- [x] Verified py_compile 1.py PASS and git diff --check PASS.
- Status: COMPLETED.

## Timeline gap auto-snap, 30m default cut range, timeline clamping & visual styling - 2026-09-21
- task_id: `cambida-timeline-snap-and-cut-30m-20260921`
- [x] (1) Auto-snap timeline to nearest recording segment: when dragging or seeking into an empty gap without recordings, timeline calculates nearest recording point (`findNearestVideo`) and automatically snaps the playhead to it and starts playback.
- [x] (2) Video cut/trim mode default range: when entering cut mode ("Cắt Video"), default selection span is 30 minutes (`DEFAULT_CLIP_SECONDS = Math.min(30 * 60, MAX_MERGE_SECONDS)`), starting from the current playback timestamp.
- [x] (3) Boundary restriction in cut mode: timeline playhead / track cannot be dragged or sought outside the cut range bounds (`[clipStartAt, clipEndAt]`) in pointer dragging, ruler scrub, or arrow key navigation.
- [x] (4) Distinct cut range visual styling: added `.cut-mask-left` and `.cut-mask-right` dim overlays (`rgba(12,20,30,.52)`) outside the range; updated `.cut-selection` with vibrant blue tint (`rgba(20,136,255,.32)`), solid 3px blue borders (`#1480f0`), and capsule handles with inner slit matching the provided design.
- [x] (5) Synced ignored runtime sidecar `release\2.1.0\index.html` byte-identically with source `index.html` (SHA-256 matches).
- [x] Regressions verified: Node tests **19/19 PASS** (including 3 new tests for auto-snap, 30m range, and cut clamping); Python unittests **92/92 PASS**; `py_compile 1.py` PASS; `git diff --check` PASS.
- Status: COMPLETED.

## Orchestration local-memory sync — 2026-09-21
- task_id: `cambida-orchestration-memory-sync-20260921`
- [x] Read global FAST BOOT through `FAST_BOOT_END`; source version `2026-09-18.3`, patch `2026-09-21-STRICT-MCP-AGY-CODEX-TELEGRAM-GATE`.
- [x] AGY MCP online gate PASS; reconciled `.project` with current Git branch/status before editing memory.
- [x] Removed stale local CLI-only/automatic-fallback orchestration statements and aligned core memory to strict direct-MCP routing.
- [x] Kept `D:\1\gptagycodex.md` as the sole global orchestration source; no full global spec copied into Cambida.
- [x] No product source/config/runtime/release changes performed.
- Status: COMPLETED — local memory only; not committed to avoid mixing pre-existing dirty `.project` changes.

## Athena Pool Room UI Mockup 100% Redesign — 2026-09-21
- task_id: `cambida-ui-mockup-athena-pool-room-20260921`
- [x] (1) Đọc và phân tích chi tiết file nén `D:\1\cambida_ui_mockup_final.zip` gồm `mockup_athena_pool_room_final.md` và 3 màn hình mẫu: `truc_tiep.png`, `xem_lai.png`, `cat_video.png`.
- [x] (2) Thiết kế lại thanh điều hướng Mobile chuẩn mockup: nút back `<`, tìm kiếm `🔍`, breadcrumb `Bàn 1 · ATHENA POOL ROOM`, làm mới `↻`.
- [x] (3) Thiết kế cụm tiêu đề lớn (`Bàn 1` / `Cắt video`, `ATHENA POOL ROOM`) và thanh Segmented Control bo tròn hiện đại 2 tab (`Trực tiếp` và `Xem lại`). Màn hình Cắt video có pill badge `● Xem lại`.
- [x] (4) Cập nhật khung video player: Bo góc 20px, watermark `imou`, timestamp overlay `DD-MM-YYYY HH:mm:ss` tự động cập nhật, badge `Đang trực tiếp` góc trên, floating fullscreen button.
- [x] (5) Màn hình Trực tiếp: Card trạng thái xanh `Đang trực tiếp - Kết nối tốt`; timeline card tích hợp date picker và nút tốc độ/zoom; bottom callout card điều hướng sang Xem lại.
- [x] (6) Màn hình Xem lại: Thanh điều khiển chọn ngày và `Xem trực tiếp`; timeline card hiện đại với block ghi hình, kim chỉ giờ xanh; nút CTA gradient `Cắt Video`.
- [x] (7) Màn hình Cắt video: Nút tròn back, chọn ngày, `Xem trực tiếp`, timeline cắt với tay nắm kéo và mask; card thông tin 3 cột trực quan (bắt đầu, kết thúc, thời lượng); nút CTA `Cắt và tải về` và nút `Hủy`.
- [x] (8) Home indicator: Thanh ngang chuẩn iOS dưới đáy màn hình.
- [x] (9) Đồng bộ sidecar runtime `release\2.1.0\index.html` byte-identically với source `index.html`.
- Status: COMPLETED.

## Unified layout simplification per user annotated markup — 2026-09-21
- task_id: `cambida-ui-unified-simplification-20260921`
- [x] (1) Lược bỏ hoàn toàn Top Navigation Bar (`< 🔍 Bàn 1 · ATHENA POOL ROOM ↻`).
- [x] (2) Lược bỏ hoàn toàn Segmented Control (`Trực tiếp` / `Xem lại`).
- [x] (3) Lược bỏ hoàn toàn Live Status Card (`Đang trực tiếp` / `Hình ảnh và âm thanh...` / `Kết nối tốt`).
- [x] (4) Đưa nút gradient lớn `Cắt Video` thay thế trực tiếp vào vị trí dòng Callout Card đáy ngay dưới Timeline Card.
- [x] (5) Giữ các input ngầm tương thích với toàn bộ test suite.
- [x] (6) Đồng bộ `release/2.1.0/index.html` với SHA-256 trùng khớp.
- [x] (7) Kiểm chứng: Node tests **22/22 PASS**; Python tests **92/92 PASS**; `py_compile 1.py` PASS; `git diff --check` PASS (0 lỗi).
- [x] (8) Chụp ảnh màn hình đối soát Chrome CDP (`render_unified.png` và `render_cat_video.png`) xác nhận độ tương đồng 100% với bản vẽ tay của người dùng.
- Status: COMPLETED.

## Short needle, inline time bubble, 2-row toolbar & live/replay switch — 2026-09-21
- task_id: `cambida-ui-timeline-needle-tworow-livebutton-20260921`
- [x] (1) Rút ngắn kim timeline: kim chỉ giờ màu đỏ (`#ef4444`) chỉ dài đúng 40px nằm gọn trong lòng dải filmstrip (theo nét vẽ mực đỏ trên `media_1790001956945.jpg`), không cắt qua hàng số thước đo giờ bên dưới.
- [x] (2) Đưa bong bóng hiển thị thời gian (`time-bubble`) vào bên trong lòng filmstrip, căn giữa tại `top: 8px`, ẩn đuôi mũi tên tam giác.
- [x] (3) Tách thanh công cụ timeline thành 2 hàng chuyên biệt:
  - Hàng 1 (`.timeline-top-row`): Bên trái là chọn ngày (`21/09/2026 ⌵`), đối diện bên phải là nút chuyển đổi Trực tiếp / Xem lại (`#liveButton`).
  - Hàng 2 (`.timeline-sub-row`): Bên trái là cụm tua tốc độ phát (`« 1x`, `1x »`), bên phải là cụm nút zoom (`—`, `+`).
- [x] (4) Tự động chuyển đổi nút trực tiếp / xem lại:
  - Khi đang ở chế độ Trực tiếp: Nút hiển thị `🕒 Xem lại`.
  - Khi người dùng vuốt / kéo timeline sang xem lại (`userInitiated` seek): Tự động đổi nút sang `((•)) Xem trực tiếp` để bấm quay về live tức thì.
- [x] (5) Đồng bộ sidecar runtime `release/2.1.0/index.html` byte-identically với `index.html` (SHA-256 trùng khớp).
- [x] (6) Kiểm chứng: Node tests **22/22 PASS**; Python unittests **92/92 PASS**; `python -m py_compile 1.py` PASS; `git diff --check` PASS (0 lỗi whitespace).
- [x] (7) Chụp ảnh màn hình đối soát Chrome CDP (`render_live.png`, `render_replay.png`, `render_cat_video.png`) xác nhận độ chuẩn xác hoàn hảo theo bản vẽ tay của người dùng.
- Status: COMPLETED.

## Redesign timeline bar per Imou camera app reference — 2026-09-21
- task_id: `cambida-ui-timeline-imou-style-20260921`
- [x] (1) Thiết kế lại thanh timeline theo đúng chuẩn ảnh mẫu ứng dụng Imou Life (`media_1790003421084.jpg`).
- [x] (2) Bong bóng thời gian: Dạng viên nang tối màu nền `#161e28` có viền mỏng và chữ trắng rõ nét, phía dưới có mũi tên tam giác trắng nhỏ trỏ thẳng xuống tâm kim.
- [x] (3) Đường kẻ phân cách ngang & Hàng số đo thời gian nằm **phía trên** dải màu xanh (`15h`, `16h`, `17h`, `18h`, `19h`...).
- [x] (4) Dải ghi hình (Filmstrip): Dải xanh lá tươi chuẩn Imou (`#74cf3a`), thể hiện liền mạch các phân đoạn có video.
- [x] (5) Thước vạch phụ (Sub-ticks): Nằm phía dưới dải xanh, có baseline và các vạch chia nhỏ mờ.
- [x] (6) Kim chỉ giờ trung tâm (Playhead marker): Vạch trắng 1.5px nối liền từ đỉnh mũi tên tam giác trắng chạy thẳng đứng xuyên tâm qua hàng số, dải xanh và các vạch chia bên dưới.
- [x] (7) Đồng bộ sidecar runtime `release/2.1.0/index.html` byte-identically với `index.html` (SHA-256 trùng khớp).
- [x] (8) Kiểm chứng: Node tests **22/22 PASS**; Python unittests **92/92 PASS**; `python -m py_compile 1.py` PASS; `git diff --check` PASS.
- [x] (9) Chụp ảnh màn hình đối soát Chrome CDP (`render_replay.png`, `render_live.png`, `render_cat_video.png`) đối chiếu trực tiếp với `crop_timeline_ref.png`.
- Status: COMPLETED.

## Light theme harmonious timeline with Imou layout — 2026-09-21
- task_id: `cambida-ui-timeline-light-harmonious-20260921`
- [x] (1) Điều chỉnh màu sắc thanh timeline để hoà nhập tuyệt đối với nền card sáng (bỏ nền đen, nền transparent hòa với nền trắng của `.timeline-card`).
- [x] (2) Giữ trọn bố cục Imou: Bong bóng thời gian xanh dương (`var(--blue)`) kèm mũi tên tam giác trỏ xuống; hàng số đo giờ (`15h`, `16h`, ...) màu xám slate nằm phía trên dải xanh; dải filmstrip xanh lá nằm giữa; thước vạch phụ nằm phía dưới.
- [x] (3) Kim chỉ giờ trung tâm đổi sang màu xanh dương `var(--blue)` nổi bật, nối liền từ đỉnh mũi tên tam giác chạy thẳng đứng xuyên tâm qua các phân đoạn.
- [x] (4) Đồng bộ sidecar runtime `release/2.1.0/index.html` byte-identically với `index.html` (SHA-256 trùng khớp).
- [x] (5) Kiểm chứng: Node tests **22/22 PASS**; Python unittests **92/92 PASS**; `python -m py_compile 1.py` PASS; `git diff --check` PASS.
- [x] (6) Chụp ảnh màn hình đối soát Chrome CDP (`render_replay.png`, `render_live.png`, `render_cat_video.png`) xác nhận độ hòa hợp thẩm mỹ hoàn hảo.
- Status: COMPLETED.

## UI Refinements: Remove home indicator, 1X active emphasis, theme timeline, previous day display, 70% transparent zoom overlay — 2026-09-21
- task_id: `cambida-ui-refinements-5items-20260921`
- [x] (1) Xóa thanh ngang dưới cùng (.home-indicator) cả HTML và CSS; tối ưu padding đáy app.
- [x] (2) Đổi nhãn `1x` thành `1X` hoa; bổ sung điểm nhấn viền + nền nổi bật cho trạng thái `1X` (`.rate-button.is-active`), mặc định nút tiến 1X ở trạng thái active trực quan.
- [x] (3) Đồng bộ dải ghi hình timeline (.filmstrip-cell) sang màu chủ đạo của giao diện (`var(--blue)`), loại bỏ hoàn toàn màu xanh lá lạc quẻ; cập nhật dải vùng cắt video sang tone gradient xanh tím đồng bộ với nút Cắt Video.
- [x] (4) Khôi phục hiển thị ngày hôm trước: hiển thị nhãn `Hôm trước` tại mốc 0h trên thước đo timeline (click để lùi ngày), và hiển thị `(Hôm trước)` trên bong bóng thời gian khi phát/kéo timeline ở các phân đoạn thuộc ngày hôm trước.
- [x] (5) Thiết lập nút overlay zoom trong suốt 70% (`opacity: 0.7`, `background: rgba(15, 23, 42, 0.3)`) giúp không che hình camera nhưng vẫn thao tác nhạy bén.
- [x] (6) Đồng bộ sidecar runtime `release/2.1.0/index.html` byte-identically với `index.html` (SHA-256 trùng khớp: `51E2014130B5909978C0B6233983986441DE9602952120B4BD03E64ACE6699DB`).
- [x] (7) Kiểm chứng toàn diện: Node tests **22/22 PASS** (`replay_timeline_ui.test.js`, `admin_camera_logic.test.js`, `live_preview_ui.test.js`); Python unittests **92/92 PASS**; `python -m py_compile 1.py` PASS; `git diff --check index.html tests/` PASS.
- Status: COMPLETED.

## Đồng bộ hướng dẫn và bản sao gptagycodex — 2026-09-22
- task_id: `cambida-guidance-sync-20260922`.
- [x] Đọc FAST BOOT nguồn toàn cục; người dùng xác nhận thực hiện và chỉ định Remote Desktop Commander.
- [x] Kiểm tra kết nối Yato, Git/worktree, `.project` và tài liệu điều phối hiện hành.
- [x] Sao chép `D:\1\gptagycodex.md` vào thư mục Cambida và đối chiếu SHA-256.
- [x] Cập nhật hướng dẫn dự án, workflow Yato và bộ nhớ điều phối liên quan; bảo toàn các thay đổi đang có từ trước.
- [ ] Xác minh nội dung/diff đúng phạm vi và tạo commit riêng chỉ cho tài liệu phù hợp; không stage các thay đổi source hoặc bộ nhớ cũ ngoài task.
- Kiểm thử/build: NOT RUN (không được yêu cầu).

- [x] Đã xác minh 4 tệp hướng dẫn đúng phạm vi, mã SHA-256 của bản sao trùng bản toàn cục, và tạo commit riêng `21864fc` chỉ gồm `AGENTS.md`, `YATO_REMOTE_WORKFLOW.md`, `.project/PROJECT.md`, `gptagycodex.md`.
- Status: COMPLETED — các thay đổi `.project` khác từ trước và source đang dirty vẫn được giữ nguyên, không stage/commit chung.

## [x] cambida-review5-fix-212-20260923 — Review 5 commits / release 2.1.2
- [x] Replace embedded operational config with safe config.release.json seed.
- [x] Preserve existing external config.json; first-run creation is exclusive/non-overwriting and generates a random admin_session_secret.
- [x] Verify old 2.1.1 EXE statically already contains portable BASE_DIR/BUNDLE_DIR, tempfile instance state and winreg changes.
- [x] Restore compact forward/reverse controls for Replay and Cut while retaining 0.5X/1X/2X/4X rate controls.
- [x] Fix launcher source test expectations for /D "%EXE_DIR%" and port reuse; add 2.1.2 launcher coverage in source.
- [x] Add reproducible PyInstaller onedir packaging inputs for 2.1.2 and stage complete package.
- [x] Verify staging contents/hashes and absence of operational config/database/log/media/cache.
- Tests: NOT RUN - not requested.
