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

## 2026-09-20 - Replay seek/live guard + launcher
- Task cambida-replay-launcher-20260920: manual timeline scrub now selects exact recorded datetime and autoplays; fixed-center needle, coverage/gaps, Cut, speed and zoom are retained.
- playerRequestSerials rejects stale async metadata callbacks. Programmatic live initialization/list refresh never loads replay; user scrub while live exits live and replays the selected target.
- Xem truc tiep remains /cam<id>?stream=auto... and only moves the timeline visually.
- Exact double-click path: D:\1\cambida\CCTV_2.1.0.launcher.cmd. It reuses TCP 8004, starts release\2.1.0\CCTV_2.1.0.exe only when needed, waits for readiness, and opens the default browser without killing a process.
- Verification: Node 9/9, Python 81/81, launcher 3/3, py_compile and diff-check PASS. Source/sidecar HTML hashes match.
- PyInstaller rebuild blocked before artifact output by WinError 5 reading C:\Users\qwusv\AppData\Roaming\Python\Python313\site-packages; no user server restart or config/media change applied.
- Codex could not stage/commit due to restricted execution token; orchestrator staged task-scoped paths successfully, and commit is being finalized: git add could not create .git\\index.lock because the execution token is denied write/delete on .git; no ACL, lock, or unrelated process was changed.


## 2026-09-21 — Khôi phục sản phẩm về 042b9d8
- Parent task `cambida-restore-042b9d8-20260921`: verified source/release/runtime; target product commit `042b9d8` (2026-09-21 00:15:41 +07); rollback commit `a9d4120329cb065370c44c0aa19f80ef77bb30fb` preserves later orchestration docs.
- 8 source/test paths match target Git blobs; `tests/test_stream_copy_merge.py` unchanged; product Git diff from 042b9d8 empty (excluding AGENTS.md). Pre-existing dirty `.project` and other untracked files preserved.
- Backup before restore: `D:\1\cambida\backup\rollback-to-042b9d8-20260921-013230` (source, config, database, old executables/runtime). No video reset/deletion.
- Gates: Python unittest 92/92 PASS, Node UI 14/14 PASS, static syntax and Git diff-check PASS. PyInstaller onedir rebuild PASS with 10 Dahua NetSDK DLLs.
- Both official release\2.1.0 EXEs (root and nested compatibility) hash `8D8796356F91565F4C110E857F86B8FCDC65E40BC506A29B824406E64D632567`. Source/root-release index.html and admin.html hashes match. Root/workspace config unchanged hash `15D07D038704D61C76C43C8762BA31CFD58E8083CD2F9842E41EB7AFB035B5E3`; nested compatibility config preserved as-is.
- Previous source server PID 9968 exited before orchestration restart; restored source server PID 32940 owns port 8004. HTTP `/`, `/live`, `/timeline`, `/replay/cam1` = 200; no b6fe156 auto-live marker. After restart Cam 1 generated `cam1_01-41-40_to_01-42-40_(21-09-2026).mp4`; FFmpeg decode-to-null exit 0.
- Source startup logs pre-existing `winreg` undefined warning in Windows autostart setup at exact target commit. Real browser GUI flow and launch of packaged EXE not individually exercised; do not claim these passed.

## 2026-09-21 - Timeline hien thi ngay hom truoc va day rollover
- Task cambida-timeline-prevday-20260921:
  - Timeline ruler tai moc 0h doi thanh 'Hom truoc', ho tro click chuyen ve ngay hom truoc (changeTimelineDay(-1)).
  - Keo timeline sang trai vuot moc 0h (rawProgress < 0) tu dong chuyen ve ngay hom truoc va tua den moc tuong ung.
  - Tich hop nut chuyen ngay previous/next cho ca man hinh xem lai va cat video.
  - Mac dinh zoom timeline ve 1 (toan bo 1 ngay).
- Ignored runtime sidecar release\2.1.0\index.html dong bo chinh xac voi source (SHA-256: D07E34FF37998ED2C759C9288ADF8A552F4BCDC28EE66213A8C77AB686F70663).
- Kiem thu: Node tests 18/18 PASS, Python unittests 92/92 PASS, py_compile 1.py PASS, git diff --check PASS.
- Task-scoped commit: b28a546. Status: COMPLETED.

## 2026-09-21 - Replay UI streamlining, continuous timeline ruler, instant rate and initial live view
- Task cambida-replay-streamline-20260921:
  - Loai bo play button va time readout duoi video; loai bo zoom buttons; chuyen nut fullscreen thanh overlay button (.video-overlay-button) ben trong video frame.
  - Xoa prefix 'Hom nay, ' trong date label; xoa chu 'Hom truoc' tren timeline ruler.
  - Dat nut tua tien (1x >>) ngay ben canh nut tua lui (<< 1x) o ben trai timeline toolbar.
  - Xu ly chu ky toc do phat ngay lan bam dau tien (2x -> 4x -> 1x -> 2x), bo click chet.
  - Thanh timeline 48h lien mach (... 22h 23h 24h 0h 1h 2h ...) qua moc nua dem, hien thi lien tuc ban ghi cua hom truoc va hom nay.
  - Xoa nut 'Tai doan nay ve may'; doi 'Bat dau cat video' thanh 'Cat Video' voi visual primary blue gradient.
  - Xem truc tiep mo ngay tai trang hien tai, khong mo trang moi.
  - Mo trang mac dinh luong dau tien la xem truc tiep (setScreenLive('replay', true)), scrub timeline chuyen sang xem lai.
- Ignored runtime sidecar release\2.1.0\index.html dong bo chinh xac voi source index.html (SHA-256: FB24CF4359BE673C4B7C105F46A76D9B05B27CB632BF0682283FF03D25E599A6).
- Kiem thu: Node tests 18/18 PASS, Python unittests 92/92 PASS, py_compile 1.py PASS, git diff --check PASS.
- Task-scoped commit cho index.html va tests/replay_timeline_ui.test.js. Status: COMPLETED.

## 2026-09-21 - Restore zoom, 5h default zoom, admin server restart, and silent startup
- Task cambida-zoom-admin-restart-silent-20260921:
  - Khoi phuc cum nut zoom timeline (#timelineZoomOut, #timelineZoomIn, #cutTimelineZoomOut, #cutTimelineZoomIn) tren ca replay va cut toolbars.
  - Zoom mac dinh hien thi khoang 5 gio tren viewport (DEFAULT_TIMELINE_ZOOM = 48 / 5 = 9.6).
  - Trang admin co nut 'Khoi dong lai Server' o ca Trung tam van hanh va thanh luu sticky-save, goi POST /api/admin/restart va tu dong poll trang thai cho toi khi online.
  - Backend 1.py bo lenh tu dong bat trinh duyet khi khoi dong server; tray icon them 'Mo Cambida' mac dinh khi double click; single-instance cho phep double click mo web khi server da chay.
- Synced release\2.1.0\index.html voi source index.html.
- py_compile 1.py PASS, git diff --check PASS. Status: COMPLETED.

## 2026-09-21 - Server restart verification
- Server da duoc khoi dong lai thanh cong tren port 8004.
- Tien trinh cu PID 34424 da duoc dung; tien trinh moi chay ma nguon moi nhat voi day du cac tinh nang:
  - Khong tu dong bat trinh duyet khi boot.
  - API /api/admin/restart da hoat dong.
  - Timeline zoom 5h mac dinh va cum nut zoom da kich hoat.
- Kiem tra HTTP: GET / va /replay/cam1 deu tra ve 200 OK; /api/status hoat dong dung phan quyen admin.

## 2026-09-21 - Toi uu do tre khoi dong luong truc tiep NetSDK (cambida-live-latency-opt-20260921)
- Scope: Toi uu pipeline giai ma xem truc tiep NetSDK trong `dahua_37777.py` (ham `iter_jpeg_frames`).
- Nguyen nhan goc re da xac minh:
  - FFmpeg thieu cau hinh probesize/analyzeduration nen mac dinh mat 5s tham do packet.
  - `CLIENT_RealPlayEx` goi truoc khi khoi tao tien trinh FFmpeg va callback lam mat I-frame dau tien (t=0.08s), buoc FFmpeg phai doi chu ky I-frame ke tiep (4.0s).
  - Co `+nobuffer` khien packet I-frame tham do bi huy trong decoder, gay tre tich luy 8.5s va burst 60 frame don dap.
- Xu ly:
  - Khoi tao tien trinh `ffmpeg.exe` va worker threads truoc khi kich hoat `RealPlayEx`.
  - Them tham so `-probesize 16384 -analyzeduration 0 -flags low_delay` cho FFmpeg; bo `-fflags +nobuffer`.
  - Goi `CLIENT_MakeKeyFrame` ngay khi mo luong de bat buoc camera phat I-frame tuc thi.
  - Giu nguyen 100% ham `record_segment` va luong ghi hinh MP4 60s Main stream khong bi anh huong.
- Kiem thu thuc te:
  - Thoi gian mo khung hinh dau tien: giam tu **8.5s xuong 0.839s** (nhanh gap 10 lan).
  - Khong con hien tuong xa don 60 frame; luong phat on dinh ~10 fps, do tre thuc te giu o muc < 0.5s.
  - Luong ghi hinh Cam 1 tao segment `cam1_04-05-42_to_04-06-42_(21-09-2026).mp4` dung 60.00s, video 1080p H.264 + AAC hoan toan on dinh.
  - `python -m unittest discover -s tests`: 92/92 PASS; `py_compile`: PASS; `git diff --check`: PASS.
- Commit scoped: `0b7a585`. Status: COMPLETED.

## 2026-09-21 - Them bieu tuong loading khi cho video live (cambida-live-loading-spinner-20260921)
- Scope: Bổ sung biểu tượng loading spinner và thông điệp trạng thái khi chờ luồng xem trực tiếp sẵn sàng.
- Thay đổi:
  - Thêm cấu trúc HTML `<div id="liveLoading">` và `<div id="cutLiveLoading">` kèm CSS spinner xoay tròn tinh tế nằm giữa player frame.
  - Cập nhật hàm `setScreenLive`: hiện spinner khi bắt đầu kết nối; tự động ẩn khi khung hình đầu tiên nạp xong (`onload`) hoặc khi lỗi (`onerror`).
  - Ẩn spinner khi quay về chế độ xem lại.
  - Đồng bộ file sidecar runtime `release/2.1.0/index.html`.
- Kiểm thử:
  - Node tests: 19/19 PASS (bao gồm test mới `live stream shows loading indicator while connecting and hides it on load`).
  - Python tests: 92/92 PASS.
  - `git diff --check`: PASS.
- Commit scoped: `00b7421`. Status: COMPLETED.

## 2026-09-21 - Server restart fix and confirmation
- Nang cap co che restart_server trong 1.py voi wrapper timeout 1s tre truoc khi goi tien trinh moi, giup tien trinh cu va socket tren port 8004 giai phong hoan toan.
- Bo sung vong lap retry ket noi socket trong run_server (thu lai toi da 20 lan x 0.5s neu gap WinError 10048).
- Server hien tai dang chay on dinh (PID 36000, listening 0.0.0.0:8004).
- HTTP GET / va /replay/cam1 deu tra ve 200 OK.

## 2026-09-21 - Fix live spinner and eliminate broken image icon (cambida-live-spinner-broken-img-fix-20260921)
- Scope: Sửa lỗi không hiển thị loading spinner và xuất hiện biểu tượng vỡ ảnh ("Hình ảnh trực tiếp camera 1") trên trình duyệt di động (Cốc Cốc Android).
- Nguyên nhân:
  - Tiến trình cũ (PID 29380) chạy từ 04:05 trước khi commit 00b7421 được tạo, khiến Flask cache template index.html cũ trong RAM (has liveLoading: False).
  - Thẻ `<img>` được bật `hidden = false` ngay khi bắt đầu gọi URL, khiến trình duyệt hiển thị icon ảnh vỡ nếu gặp lỗi mạng hoặc đợi frame đầu tiên.
- Xử lý:
  - Giữ `stream.hidden = true` trong suốt lúc chờ kết nối, chỉ hiện `liveLoading` spinner xoay tròn giữa khung phát.
  - Khi khung hình đầu tiên nạp thành công (`stream.onload`), tự động ẩn spinner và mở `stream.hidden = false`.
  - Nếu xảy ra lỗi mạng (`stream.onerror`), ẩn spinner, giữ `stream.hidden = true` (không làm lộ icon ảnh vỡ native) và báo lỗi trực quan.
  - Cập nhật test `replay_timeline_ui.test.js` kiểm tra chặt chẽ thuộc tính `hidden` của `liveStream` trong toàn bộ vòng đời.
  - Đồng bộ `release/2.1.0/index.html`.
  - Khởi động lại máy chủ (PID 36000), nạp mã tối ưu NetSDK và giao diện mới.
- Kiểm chứng:
  - Node tests: 19/19 PASS.
  - Python tests: 92/92 PASS.
  - Probe thực tế: HTTP GET `/replay/cam1` trả về `has liveLoading: True, has live-spinner: True`.
  - Probe luồng: `/cam1?stream=sub&view=live_preview` trả về `200 OK` (multipart/x-mixed-replace), frame JPEG đầu tiên trong 1.39s - 2.2s.
  - Luồng ghi hình tiếp tục hoạt động liên tục, xuất file `cam1_04-24-50_to_04-25-50_(21-09-2026).mp4` (2.4 MB) đầy đủ 60s.
- Status: COMPLETED.

## 2026-09-21 - Ultra-low live latency 0.5s & Fix frame overflow 4:3 contain (cambida-live-opt-contain-20260921)
- Scope:
  1. Tối ưu độ trễ mở luồng xem trực tiếp xuống mức tức thì (~0.5s thay vì ~2s).
  2. Khắc phục ảnh live bị quá khung hình/cắt xén phần trên dưới do tỷ lệ 4:3 của sub stream bị `object-fit: cover` phóng to.
  3. Cập nhật thông điệp loading từ "Đang mở trực tiếp..." thành "Đang tải...".
- Xử lý:
  - `dahua_37777.py`: Đổi tham số FFmpeg sang `-probesize 4096` và xuất khung hình theo nhịp gốc trực tiếp (bỏ bộ lọc `-vf fps` gây đệm frame). Độ trễ mở luồng giảm xuống còn **0.53s** (nhanh gấp ~15 lần so với ban đầu 8.5s).
  - `index.html`: Đổi CSS `.player video, .player .live-stream` sang `object-fit: contain;` giúp thu trọn vẹn 100% trường nhìn của camera (chuẩn 640x480 tỷ lệ 4:3), không còn bị mất hay tràn góc trên/dưới.
  - Sửa text nhãn chờ trong `#liveLoading` và `#cutLiveLoading` thành `Đang tải...`.
  - Đồng bộ `release/2.1.0/index.html`.
  - Khởi động lại dịch vụ máy chủ web sạch sẽ (PID 16440).
- Kiểm chứng:
  - Node tests: 19/19 PASS.
  - Python tests: 92/92 PASS.
  - Probe thực tế: `/replay/cam1` trả về `object-fit:contain` và `Đang tải...`.
  - Probe luồng: Mở luồng live trong 0.53s - 0.55s.
- Status: COMPLETED.

## 2026-09-21 - Full width 4:3 live player & 18px rounded corners (cambida-live-fullwidth-rounded-20260921)
- Scope:
  1. Phóng to khung hình live vừa khít 100% chiều ngang màn hình điện thoại, tự động co giãn theo tỷ lệ 4:3 thực tế của luồng sub camera (640x480), loại bỏ hoàn toàn viền đen 2 bên. Các thành phần điều khiển bên dưới tự động dịch chuyển lên xuống theo chiều cao khung hình.
  2. Bo cong 4 góc mềm mại 18px (thay vì góc nhọn) cho cả Xem lại và Trực tiếp, bổ sung mask chống tràn góc nhọn khi phần cứng render trên Android/Cốc Cốc.
- Xử lý:
  - `index.html`:
    - `.player`: `border-radius: 18px; aspect-ratio: 16/9; isolation: isolate; -webkit-mask-image: -webkit-radial-gradient(white,black); mask-image: radial-gradient(white,black);`.
    - `.player video, .player .live-stream`: `border-radius: inherit;`.
    - `.screen.is-live .player`: `aspect-ratio: 4/3;`.
    - `setScreenLive`: Khi `stream.onload` nạp xong, gán `frame.style.aspectRatio = \`${stream.naturalWidth}/${stream.naturalHeight}\`` để tự động vừa khít mọi tỷ lệ camera; khi thoát live trả về tỷ lệ mặc định.
  - Đồng bộ `release/2.1.0/index.html`.
- Kiểm chứng:
  - Node tests: 19/19 PASS.
  - Python tests: 92/92 PASS.
  - HTML reload xác minh: `border-radius:18px` và `.screen.is-live .player{aspect-ratio:4/3}` đã active.
- Status: COMPLETED.
