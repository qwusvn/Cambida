# PROJECT CHANGELOG — Cambida

## 2026-09-12

- Cập nhật quy tắc Git theo yêu cầu mới nhất: mọi task chỉ được chuyển sang `COMPLETED` sau khi có Git commit riêng cho đúng scope; không stage/commit thay đổi ngoài scope; báo cáo `COMMIT` bắt buộc có hash khi hoàn tất.
- Đồng bộ lại quy chuẩn điều phối mới nhất: bổ sung thứ tự ưu tiên khi xung đột `user mới nhất → project → source/Git → .project → CodeGraph → quy chuẩn chung → thông tin cũ`.
- Bổ sung vai trò CodeGraph: chỉ dùng làm code intelligence, ưu tiên trước task coding/phân tích lớn khi có `.codegraph`, không tự `codegraph init`, không ghi đè source/Git.
- Giữ Codex MCP là executor coding/core mặc định (`gpt-5.6-luna`, reasoning `max`); AGY MCP cho khảo sát/UI/module độc lập; Yato cho system/ADB/process/recovery; Remote cho GUI/visual.
- Giữ bắt buộc `task_id` cho Codex/AGY, chuẩn trạng thái agent, timeout 10 phút, cancel/recovery đúng task/process tree và checkpoint theo `.project`.
- Khởi tạo thư mục `.project` làm bộ nhớ chính của dự án.
- Thay `AGENTS.md` bằng quy chuẩn điều phối mới.
- Retire `PROJECT_SYNC_PROTOCOL.md` cũ; không còn dùng manifest/Drive queue/sync barrier/ACK.
- Chuyển `SYNC_STATE.json` thành compatibility tombstone.
- Ghi nhận và bảo toàn thay đổi Git có sẵn tại `index.html` trong giai đoạn cập nhật hướng dẫn.
- Mở task riêng cho `index.html` và rework giao diện theo mockup Xem lại/Cắt video.
- Giao diện mới có player tùy biến, timeline/filmstrip, marker thời gian, vùng chọn cắt, cảnh báo H.265/HEVC, tải video gốc và cắt + tải.
- Kiểm tra Jinja parse, render template và JavaScript syntax đều đạt.
- Hoàn tất kiểm chứng trực quan bằng Chromium ở viewport 430×900 cho cả màn Xem lại và Cắt video.
- Phát hiện và sửa lỗi chức năng cắt: frontend trước đó làm tròn thời gian xuống phút và backend `/merge` chỉ concat nguyên segment.
- Frontend nay gửi `start/end` chính xác tới giây; backend dùng metadata start/end thật, cắt đúng từng phần giao nhau, hỗ trợ nhiều segment và báo khoảng trống qua SSE.
- Để đảm bảo mốc cắt chính xác thay vì phụ thuộc keyframe của stream-copy, phần được chọn được re-encode H.264/AAC rồi mới concat các part khi cần.
- Integration test cô lập: yêu cầu 60 giây → output 60.0 giây; case nhiều segment có gap 5 giây → output 50.0 giây media có sẵn + SSE notice; `/download` trả 200.
- Sửa regression UI nguồn NVR sau redesign: khôi phục nhãn `Server 1 - NVR` và `Server 2 - Local`, kể cả NVR không backup.
- Chạy toàn bộ `unittest`: 29/29 test đạt.
- Dọn script/thư mục integration tạm phát sinh trong vòng hiện tại; preview server đã dừng, cổng 8765 không còn listener.
- Chưa commit các thay đổi source.
- Khởi động vòng Codex Browser/Computer Use để tái hiện lỗi chọn ngày và kéo-thả timeline; Codex đã sửa một phần `index.html` liên quan date input và drag state nhưng chưa hoàn tất retest.
- Theo yêu cầu người dùng, đã dừng tiến trình Codex giữa task và dừng luôn preview Flask phụ trợ trên cổng 8767; trạng thái phần sửa interaction hiện là chưa xác minh hoàn tất.

## 2026-09-12 — UX/UI replay/cut interaction
- Hoàn thiện header/source selector, badge mode, live in-place, timeline scrub/zoom, tua 2x/4x, pinch zoom/pan video và giữ luồng cut/download.
- Xác nhận 29/29 unittest đạt, JS syntax đạt, git diff-check đạt.
- CDP 430x900 xác nhận không range seek riêng, không backButton, source switching và live/replay/cut interaction hoạt động, không console error/exception.

## 2026-09-12 — Timeline cố định kim
- Chỉ chỉnh phần timeline trong `index.html`: đưa kim thời gian ra khỏi track để luôn cố định giữa viewport; thao tác kéo dịch track/ruler bên dưới kim.
- Bỏ toàn bộ thumbnail khỏi timeline; thay bằng các block màu xanh theo khoảng thời gian thực của `visibleVideos`, phần không có video giữ nền trong suốt.
- Timeline chuyển sang trục 24 giờ của ngày được chọn; vùng chọn cắt và hai tay nắm được quy đổi sang thời gian tuyệt đối để không lệch sau thay đổi.
- Chromium 430×900 xác nhận marker giữ x=215 px khi track dịch 80 px, time bubble thay đổi, không còn `img` trong filmstrip và block video có màu xanh; JavaScript syntax, `git diff --check` và 29/29 unittest đều đạt.
- Trạng thái UI/timeline đã được checkpoint riêng tại commit `335bb2c` trước task chuyển chế độ để không trộn scope lịch sử với thay đổi mới.

## 2026-09-12 — Chuyển Timeline / Chọn giờ
- Thêm một nút tùy chọn trên màn Xem lại để chuyển giữa timeline mới và cách chọn thời gian kiểu cũ.
- Chế độ `Timeline` vẫn là mặc định. Chế độ `Chọn giờ` dùng ngày đang chọn, ô giờ bắt đầu và danh sách các đoạn video còn hiệu lực sau mốc giờ đó; chọn đoạn dùng lại `selectVideo` hiện tại nên không thay luồng phát/tải/cắt.
- Chromium 430×900 xác nhận chuyển hai chiều đúng, chế độ cũ chọn đúng đoạn `13:06:24 - 13:07:24` từ mốc 13:00 và player nhận `/video/fake.mp4`; khi quay lại timeline kim vẫn nằm chính giữa.
- JavaScript syntax, `git diff --check -- index.html` và unittest 29/29 đều đạt.

## 2026-09-12 — Checkpoint 17:12 +07
- Đối chiếu lại `.project`, source/Git và trạng thái executor trước khi tạo checkpoint.
- Git hiện ở `main`, working tree chưa clean; tracked modified: `1.py`, `AGENTS.md`, `PROJECT_SYNC_PROTOCOL.md`, `index.html`; còn nhiều artifact/profile kiểm chứng untracked.
- Không có Codex MCP task `RUNNING` trong `D:\1\cambida`; AGY task `agy_mty35zms_33586c` đã `COMPLETED`.
- Xác nhận task UX/UI replay/cut đã hoàn tất và đã qua kiểm chứng cuối: py_compile đạt, unittest 29/29 đạt, JS syntax đạt, diff-check đạt, CDP 430×900 đạt.
- Ghi nhận screenshot kiểm chứng `ui_cdp_replay.png` và `ui_cdp_cut.png`; chưa commit.
- Xóa TODO lỗi chọn ngày/kéo-thả timeline đã lỗi thời vì phần này đã được hoàn thiện và retest.


## 2026-09-12 — Khôi phục HTML 1.3.42 + single NVR + Telegram pin license
- Trích trực tiếp `index.html` từ `CCTV_1.3.42.exe` bằng PyInstaller archive reader; SHA-256 HTML gốc `82a96edded553dbda4eb6da1fab82609f05dd60427639cbd764a834361230cef`.
- Khôi phục giao diện 1.3.42 và chỉ ghép lớp dữ liệu cần thiết để đọc metadata NVR hiện tại (`started_at`, `end_at`, `duration_sec`, `url`, `download_url`).
- Chốt theo yêu cầu mới nhất: một NVR duy nhất đã cấu hình; bỏ toàn bộ selector/nhãn Server 1/Server 2 khỏi replay UI.
- Khôi phục license dựa trên Windows `MachineGuid` và tin nhắn ghim Telegram; fail-closed cho replay nhưng không dừng recorder/live/admin.
- Thêm `/license`, `/activate "KEY"` và watcher kiểm tra pin định kỳ.
- Verify: Python compile, JavaScript syntax, diff-check đạt; unittest 34/34 đạt.
- Commit riêng đúng scope: `0a13a5af560fc9539b9fa815ae37dcd11b44b567`. Thay đổi `/merge` và `PROJECT_SYNC_PROTOCOL.md` có sẵn từ trước không bị đưa vào commit.

## 2026-09-12 — Release 2.1.0
- Người dùng yêu cầu phát hành trực tiếp bản **2.1.0**; cập nhật `RELEASE_VERSION.txt`, `CCTV_2.1.0.spec` và `version_info_2_1_0.txt`.
- Release dùng source hiện tại, bao gồm logic `/merge` cắt chính xác tới giây đã được kiểm chứng trước đó; phần này được đưa vào commit release để binary khớp source.
- PyInstaller 6.16.0 onedir build thành công; artifact tại `D:\1\cambida\release\2.1.0\`.
- Metadata `CCTV_2.1.0.exe`: FileVersion/ProductVersion `2.1.0`; SHA-256 `136f8386b8efee5eece19f4e788a12f625803894914e8247b2037b5f89fe7e4b`.
- Kiểm chứng trước build: py_compile PASS, unittest 34/34 PASS.
- Smoke EXE config cách ly: `/`, `/admin`, `/admin/login` = HTTP 200; replay/timeline = HTTP 403 đúng license fail-closed khi không cấu hình Telegram.
- Tiến trình smoke đã dừng đúng PID, cổng test đã đóng; thư mục build/smoke tạm đã dọn.
- Commit release: `5420e54`.

## 2026-09-12 — Gỡ bản quyền khỏi 2.1.0
- Loại bỏ hoàn toàn cơ chế license Telegram ghim và MachineGuid khỏi `1.py`.
- Xóa LicenseWatcher, gate replay/timeline/cut/download và lệnh `/license`, `/activate`; Telegram vẫn giữ cảnh báo/lệnh hệ thống khác.
- Thay test license cũ bằng regression test xác nhận replay không còn bị khóa; py_compile PASS, unittest 31/31 PASS.
- Rebuild `CCTV_2.1.0.exe`; smoke config không Telegram: `/`, `/admin`, `/replay/cam1`, `/timeline` đều HTTP 200.
- SHA-256 EXE mới: `82bb7a582387e7ec4830ba57bbdecef9975019dcd61da25cc07a08335adbb423`.
- Commit: `111d403`.

## 2026-09-12 — Sửa thời gian và cắt NVR chính xác
- Test trực tiếp NVR Dahua cho thấy đồng hồ PC/NVR chỉ lệch khoảng 6 giây, không có lỗi timezone lớn.
- Xác định root cause UI: sau khi lọc theo giờ, danh sách giữ thứ tự mới nhất trước và luôn chọn index 0; mốc 22:09 vì vậy nhảy sang segment 22:30:18.
- Sửa frontend để chọn segment chứa đúng mốc giờ; playback/download NVR dùng `at=YYYY-MM-DDTHH:MM:SS`.
- Mở rộng `/merge` để camera NVR cắt trực tiếp từ NVR theo exact start/end; Dahua dùng loadfile.cgi và chunk theo playback_chunk_sec.
- Test thật Cam 2: 22:09:00 thuộc segment 22:00:00–22:29:02; cắt 22:09:00→22:09:10 trả MP4 H.264 1080p duration 10.00s.
- 34/34 unittest PASS, py_compile PASS, JS node --check PASS.
- Commit: `f16d752`.
- Deploy production 2.1.0 hoàn tất; EXE SHA-256 `f88eb22570632843f32db103598cc014fcb2595dc82a9463f2536e68c3689bbc`; localhost:8004 PASS.

## 2026-09-12 — Timeline-only replay + bảo vệ config
- Theo yêu cầu mới nhất, replay chuyển hoàn toàn sang timeline; bỏ nhập giờ và bỏ mode chọn giờ cũ.
- Playhead cố định ở giữa, timeline/ruler kéo bên dưới; NVR coverage màu xanh, gap trống, không thumbnail.
- Timeline dùng StartTime/EndTime NVR; playback/download dùng `at=` đúng mốc dưới kim sau khi thả kéo.
- Cut dùng hai mốc datetime tuyệt đối trên toàn ngày, có thể đi qua nhiều segment/gap; backend `/merge` giữ cơ chế báo gap và ghép phần có dữ liệu.
- GUI Computer Use phát hiện và sau đó xác nhận đã sửa regression Cut→Hủy→Replay làm mất coverage/ruler.
- Commit chính: `5efc105`; commit regression fix: `338a842`.
- Kiểm chứng: py_compile PASS, JS node --check PASS, 34/34 unittest PASS, GUI final PASS.
- Cross-segment Cam 2 `22:28:55→22:30:25`: gap 76s, output phần có dữ liệu H.264 1080p duration 13.21s.
- Production 8004 deploy bằng cách chỉ thay `release\2.1.0\index.html`; không thay EXE/config.
- Config workspace giữ SHA `ee7b820bea42c423acb05f15cd548c33706a6e0c981409ad320fec1085d0c8c3`; release config giữ SHA `d0dc0fff575bdc5dc1b3068d0e45861cc92cf56c2370c872fa6fd36672161a98`.


## 2026-09-13 +07 ? Direct Imou camera RTSP support
- Backed up prior source/version states to `backup/source_versions/` before resuming 2.x development.
- Added local RTSP profiles for Imou/Dahua main/sub (`cam/realmonitor`), optional ONVIF query variant, configured/custom path and legacy fallback.
- Added non-secret in-memory profile selection cache; successful recording/test profile drives preview URL selection.
- Refactored local recording to retry RTSP profiles using the real recording attempt, without an extra preflight per segment.
- Added network/auth/path failure classification and Imou Safety Code guidance without returning RTSP URLs or secrets.
- Hardened NVR isolation, RTSP error redaction, profile-cache invalidation and video-stream probe (`0:v:0`).
- Added camera tests; full suite now 46/46 PASS.
- `config.json` unchanged; no commit.


## 2026-09-13 12:20 +07 ? Imou live transport diagnosis
- Live tested the supplied Imou DDNS endpoint without persisting credentials.
- Confirmed public TCP reachability including 554/37777, but RTSP 554 resets authenticated main/sub/ONVIF sessions with WinSock `-10054` before RTSP auth/media response.
- Added RTSP reset classification and user-facing guidance for forward-port/RTSP/TLS/firewall/NAT checks.
- Added regression test; full suite increased to 47/47 PASS.
- Config unchanged; no commit.

## 2026-09-13 - Separate Dahua/Imou NetSDK/DVRIP 37777 adapter (implementation ready, live auth blocked)
- Added dahua_37777.py with official Dahua NetSDK x64 loading, secure high-level login, RealPlay media probe, DHAV recording conversion to MP4, RealData callback preview/snapshot, and normal DVRIP challenge-auth diagnostics.
- Added vendor/dahua_netsdk runtime DLL set from Dahua support.
- Integrated local_transport=netsdk into config validation/cleaning, admin camera UI, connection test endpoint, recorder, live MJPEG and snapshot routes while preserving RTSP/NVR isolation.
- Added regression coverage; full suite is now 56/56 PASS. Python compile, admin JavaScript syntax and git diff checks pass. config.json remains byte-for-byte hash-identical.
- Live target answers native Dahua 37777 but rejects the current configured credential through both NetSDK and DVRIP. 3.x release intentionally not created until real media succeeds end-to-end.

## 2026-09-19 - NetSDK/DVRIP 37777 retest
- Corrected the adapter loader so the integrated path accepts an already-resolved SDK directory and loads the official DLL.
- Retested the existing credential without persisting it: TCP 37777/native DVRIP is reachable but returns `01000100` / `001b0002`; NetSDK returns `0x80000064` password error.
- Full local gates pass at 57/57 unittest, py_compile, rendered/admin JavaScript syntax; config remains byte-for-byte unchanged.
- Live media, snapshot, live MJPEG and MP4/ffprobe remain blocked by private authentication rejection; no 3.x artifact or commit created.

- [2026-09-13 13:17:24 +07:00] COMPLETED remove-license-2.1.0: rebuilt from backup/source_versions/2.1.0_pre_restore with license gate bypassed; replaced root+nested CCTV_2.1.0.exe; functional tests skipped per user; commit NONE.


## 2026-09-19 - Camera transport cleanup
- Implemented canonical, transport-disjoint camera configuration for Local NetSDK, Local RTSP, and NVR; removed implicit RTSP fallback from explicit NetSDK paths and aligned replay/list/recording source routing.
- Added backend/frontend regressions for mode switching, mixed payload rejection, add/remove, zero cameras, reload, NVR isolation, and rendered admin field visibility.
- Backed up and cleared all camera/table entries in the active workspace/release config copies at backup/camera-config-clean-20260919-141258; preserved global settings, videos, and separate NVR configuration.
- Local verification: 63/63 unittest, rendered admin JS 1/1, py_compile, diff-check, and authenticated source-runtime 8004 zero-channel smoke PASS. No live media proof or 3.x bump.

## 2026-09-19 - Final status
- Camera transport cleanup implementation and verification completed, including zero-channel active-config cleanup and restricted backup.
- Status: COMPLETED after the task-scoped commit. No 3.x release/version bump and no unrelated process termination.

## 2026-09-19 — Đồng bộ quy tắc toàn cục

- Thay AGENTS.md cũ bằng hướng dẫn dự án tham chiếu nguồn toàn cục duy nhất `D:\1\gptagycodex.md` và FAST BOOT.
- Bỏ quy tắc direct AGY/Codex MCP, tự fallback, timeout/hủy vì im lặng, và Telegram liên kết hội thoại; áp dụng confirmation/comma bypass, strict tool gate, CLI-only, checkpoint 15 phút, verified parent terminal.
- Cập nhật PROJECT, DECISIONS, STATE, TASKS, HANDOFF; bảo toàn mọi source/config/release và những thay đổi chưa commit đã tồn tại.
- Kiểm tra diff-check đạt; commit riêng AGENTS.md: `f7a5246`. Các tệp `.project` còn thay đổi cũ nên không commit gộp.

## 2026-09-19 — Finalize all completed camera/Imou/NetSDK work

- Hoàn tất tích hợp dirty product work về canonical camera transports, Imou/Dahua RTSP và adapter Dahua/Imou NetSDK/DVRIP 37777; giữ source, tests, admin UI và vendor runtime assets cùng một commit final.
- Cập nhật PyInstaller spec 2.1.0 để bundle 10 DLL NetSDK trong `_internal/vendor/dahua_netsdk`; UI HTML và FFmpeg vẫn phát hành cạnh EXE theo convention hiện hành.
- Verification: py_compile PASS; full unittest **63/63 PASS**; rendered/admin JavaScript **1/1 PASS**; diff-check PASS.
- PyInstaller 6.16.0 onedir và smoke từ chính EXE PASS; final release smoke `/`, `/admin/login`, `/timeline` đều HTTP 200, cleanup PASS.
- Artifact `D:\1\cambida\release\2.1.0\CCTV_2.1.0.exe` cùng nested copy có SHA-256 `E5F23EE373665FED3149569C7AB764CC7545321F0D2D1CFAF2314B5F834BC83B`.
- Giữ version 2.1.0 vì live private NetSDK media chưa có bằng chứng credential hợp lệ/snapshot/frame/MP4; config và media hiện có được bảo toàn.

## 2026-09-22 — Guidance sync
- Sao chép `gptagycodex.md` từ nguồn toàn cục vào thư mục dự án theo yêu cầu, cập nhật AGENTS.md, `.project/PROJECT.md`, YATO_REMOTE_WORKFLOW.md và dấu mốc bộ nhớ điều phối.
- Chỉ thay đổi tài liệu; không chỉnh sửa mã nguồn/config/media/release hay chạy kiểm thử/build.

## 2026-09-23 — 2.1.2 staging hardening
- Hardened first-run release config seeding and removed operational config.json from PyInstaller inputs.
- Added 2.1.2 spec, launcher, Windows version metadata and reproducible staging packaging script.
- Restored separate playback direction controls (forward/reverse) for Replay/Cut while keeping preset playback rates.
- Corrected launcher source assertions to require port reuse and /D "%EXE_DIR%".
- Created and statically verified staging onedir package at D:\1\Cambida\staging\2.1.2.
- Tests: NOT RUN - not requested.

## 2026-09-23 — iPhone Photos save reliability
- Reworked iOS file sharing so the MP4 is fetched/prepared before the save tap; the tap now calls `navigator.share({files})` immediately instead of waiting for a large async fetch and losing WebKit user activation.
- Added cancellation/cleanup of prepared iOS share blobs when leaving the completed-cut screen.
- Changed iPhone raw-video fallback to same-tab navigation and updated the on-screen instructions for HTTP / QR in-app browsers.
- No backend, packaging, config, media, release artifact, test, build or deployment changes in this task.

## 2026-09-23 — Embed replay UI into 1.py
- Added Base85+zlib embedded replay template to `1.py` with SHA-256 integrity validation.
- Changed `/replay/cam<int:cam_id>` to render the embedded replay template before falling back to external `index.html`.
- This makes the iPhone save-video replay update deployable by replacing `1.py` alone.

## 2026-09-24 — iPhone finished-video download UI
- `e0da8f5`: `1.py` bỏ nút mở video trên trang thành công; nút tải về máy đổi nhãn thành `Tải về máy` và dùng visual gradient xanh–tím.
- Bảo toàn liên kết tải MP4, trình phát và hướng dẫn; không chạy kiểm thử, build hoặc triển khai.

## 2026-09-24 - iPhone save action
- Restored former inline MP4/native sharing route for the gradient primary download button on iPhone; kept download behavior elsewhere. Commit 07a82ad; server restarted on port 8004.

## 2026-09-24 — Khôi phục 1.py về commit 41bcbd2
- Khôi phục mã nguồn `1.py` nguyên trạng tại commit `41bcbd2` (`feat(replay): embed iPhone save UI in 1.py`).
- Loại bỏ toàn bộ các điều chỉnh sau `41bcbd2` trong `1.py` (bao gồm `e0da8f5`, `07a82ad`, `c06158c`).
- Kiểm thử / build / device test: NOT RUN (không được yêu cầu).

## 2026-09-24 — Sửa nút Tải về máy mở Quick Look trên iOS
- Cập nhật `1.py`: Khi thiết bị là iOS, loại bỏ thuộc tính `download` trên nút `mergedDownloadBtn` nhưng giữ nguyên URL trỏ tới `/download/<filename>`.
- Giúp trình duyệt WKWebView/Zalo trên iPhone thực hiện điều hướng tệp đính kèm và mở màn hình Quick Look MP4 (cho phép bấm Chia sẻ -> Lưu video vào Thư viện Ảnh).
- Thêm `Cache-Control: no-store` cho trang Replay để tránh cache HTML cũ.
- Kiểm thử thiết bị: NOT RUN (chờ người dùng xác nhận trên máy thật).

## 2026-09-24 — Chuyển hướng quét QR nội bộ sang HTTPS
- Bổ sung logic redirect 302 trong `replay_cam` của `1.py`: Khi khách truy cập qua IP nội bộ (mã QR cũ tại bàn), máy chủ tự động chuyển hướng sang `public_base_url` (HTTPS).
- Cấu hình `public_base_url` trong `config.json`.
- Cho phép giữ nguyên 100% mã QR đã in trên các bàn bida; khách quét QR cũ tự động được chuyển sang HTTPS để mở khóa tính năng lưu 1 chạm vào Ảnh trên iPhone.

## 2026-09-24 — Đóng gói phát hành 2.1.5 & Thiết lập quy tắc phiên bản theo commit
- Đóng gói toàn diện phiên bản **2.1.5**:
  - Tích hợp toàn bộ gói module camera mới `camera_modules/` (Dahua/Imou, Hikvision/Ezviz, ONVIF, RTSP, Table Access, Discovery, Policy).
  - Tích hợp công tắc bảo vệ riêng tư (Privacy Switch) trên `home.html` và backend `1.py` (Bật = đỏ, Tắt = xám; khách bị chặn xem/cắt/tải khi tắt nhưng camera vẫn ghi hình ngầm 24/7).
  - Tích hợp giao diện quản lý thêm camera đa hãng trong `admin.html` (quét LAN, kiểm tra kênh probe, chọn nhiều kênh).
  - Giới hạn số bàn tối đa theo cú pháp Telegram pin `/N`.
  - Nâng cấp `RELEASE_VERSION.txt` = `2.1.5`, tạo `version_info_2_1_5.txt` (2.1.5.0), `CCTV_2.1.5.spec`, `CCTV_2.1.5.launcher.cmd`, `package_2.1.5.ps1`.
  - Biên dịch PyInstaller onedir và đóng gói sạch sẽ (Zero Config Pollution) vào thư mục `release/2.1.5` và `staging/2.1.5`.
  - Tạo tệp nén `release/2.1.5.zip` (và lưu bản sao `release/2.1.5/2.1.5.zip`).
- **Quy tắc phiên bản**: Kể từ bản 2.1.5 này, mỗi commit tiếp theo sẽ tự động tịnh tiến phiên bản mới (2.1.6, 2.1.7...).

## 2026-09-24 — Sửa lỗi cú pháp xem lại, tích hợp Cloudflared & Đóng gói 2.1.51
- Sửa lỗi cú pháp JavaScript (thừa 1 dấu đóng ngoặc `}` trong khối click của nút `iosSaveShareBtn`) trong `index.html`.
- Cập nhật lại template nhúng Base85 và SHA-256 mới trong `1.py`.
- Khắc phục triệt để lỗi trang xem lại bị ngắt JS dẫn tới ngày hiển thị `--/--/----`, giờ `--:--:--` và thước đo timeline trắng trơn trên Cốc Cốc.
- Kiểm thử hồi quy giao diện Node.js `tests/replay_timeline_ui.test.js`: **26/26 PASS**.
- Tích hợp `cloudflared.exe` và bộ công cụ cài đặt tunnel `cloudflared_setup/` vào gói phát hành.
- Đóng gói hoàn tất bản **2.1.51** vào `release/2.1.51` và `staging/2.1.51` kèm file nén `release/2.1.51.zip`.
- Khởi động lại dịch vụ máy chủ `python 1.py` trên cổng `8004` (PID 16052).

## 2026-09-24 — Chuyển trang tức thì khi cắt video, tiến trình 2 giai đoạn, pinch zoom timeline & Đóng gói 2.1.52
- Chuyển trang ngay lập tức sang #doneScreen khi bấm nút 'Cắt và tải về', loại bỏ thời gian chờ trên màn hình cắt.
- Thanh tiến trình 2 giai đoạn trực quan: Giai đoạn 1/2 hiển thị tiến trình cắt video trên server theo SSE (0%-100%); Giai đoạn 2/2 hiển thị tiến trình nạp video vào bộ nhớ RAM thiết bị qua ReadableStream (MB và %) giúp phát lại tức thì (URL.createObjectURL(blob)) và sẵn sàng cho tính năng 1 chạm lưu vào Cuộn camera iPhone qua iOS Web Share.
- Cử chỉ vuốt ngón tay phóng to/thu nhỏ Timeline (Pinch-to-Zoom): Cho phép dùng 2 ngón tay chụm/tách trên toàn thẻ timeline để zoom mượt mà từ 1x đến 96x.
- Cập nhật template nhúng Base85 và SHA-256 mới trong 1.py.
- Kiểm thử hồi quy UI 	ests/replay_timeline_ui.test.js: bổ sung test pinch zoom và done screen tiến trình 2 giai đoạn, đạt 28/28 PASS.
- Đóng gói hoàn tất phiên bản **2.1.52** vào
elease/2.1.52 và staging/2.1.52 kèm tệp nén
elease/2.1.52.zip và đầy đủ cloudflared.exe + cloudflared_setup/.
- Khởi động lại dịch vụ máy chủ python 1.py trên cổng 8004.

## 2026-09-24 — Giao diện nút tích hợp tiến trình 50/50, zoom mặc định 3h/1h & Khắc phục lỗi vô tình mở video khi zoom timeline
- **Giao diện `#doneScreen` gọn gàng**: Loại bỏ hoàn toàn các khung loading to tướng, spinner chiếm màn hình; giữ nguyên layout chuẩn (video preview và nút bên dưới).
- **Tích hợp tiến trình 50/50 trực tiếp vào nút "Lưu và chia sẻ"**:
  + Trạng thái 1: "Đang chuẩn bị video... X%" (nút tạm khóa, thanh dải màu nền `.merge-button-fill` chạy từ 0% đến 100% chia đôi):
    * 0% - 50%: Tiến trình cắt ghép video trên máy tính qua SSE.
    * 50% - 100%: Tiến trình nạp video vào bộ nhớ thiết bị qua fetch stream (ReadableStream).
  + Trạng thái 2: "Lưu và chia sẻ" (khi đạt 100%): Nút mở khóa, màu gradient tím/xanh nổi bật, sẵn sàng bấm mở bảng chia sẻ iOS (Save Video to Photos).
- **Khắc phục triệt để lỗi vô tình mở/phát video khi zoom timeline**:
  + Loại bỏ tranh chấp pointer capture và sự kiện nổi bọt trùng lặp giữa `#timelineHit` và `#timelineCard`.
  + Khi phát hiện từ 2 điểm chạm (pinch-to-zoom): hủy ngay trạng thái seek (`timelineDrag = null`).
  + Đặt cờ cử chỉ (`wasPinching`) kèm thời gian chờ (cooldown 350ms) sau khi nhấc ngón tay, triệt tiêu toàn bộ sự kiện chạm nhầm, ngăn chặn tuyệt đối việc tự động seek hay phát/mở video ngoài ý muốn.
- **Cấu hình zoom mặc định**:
  + Timeline Xem lại (`replay`): Mặc định hiển thị 3 giờ gần nhất quanh kim (`zoom = 16`, tương ứng 48h / 3h).
  + Timeline Cắt video (`cut`): Mặc định hiển thị 1 giờ tính từ vị trí cái kim (`zoom = 48`, tương ứng 48h / 1h).
- **Kiểm thử hồi quy**: Cập nhật bộ test UI `tests/replay_timeline_ui.test.js`: 28/28 PASS.
- Đồng bộ lại chuỗi Base85 và SHA-256 mới của `index.html` vào `1.py`.
- Tịnh tiến phiên bản lên **2.1.53**.
- **Đóng gói bản cập nhật gọn nhẹ 2.1.53**: Biên dịch `CCTV_2.1.53.exe` (SHA-256: `D8E0D01996104E4FBCDA46FADE2793BCA4134F965E6E0C35DA54AE2ACAD8A43E`), tạo file nén `release/2.1.53.zip` (523 KB, SHA-256: `73E6FD679342673D2D93D37AF09DE5C787AC2D743910F8FD11A960096FA3503A`) chỉ gồm các tệp cập nhật: `CCTV_2.1.53.exe`, `CCTV_2.1.53.launcher.cmd`, `Chay_CCTV.cmd`, `index.html`, `1.py`, `RELEASE_VERSION.txt`, loại bỏ toàn bộ các file tĩnh không đổi (`_internal/`, `ffmpeg.exe`, `cloudflared.exe`...).

## 2026-09-24 — Sửa mã QR bàn luôn là IP nội bộ LAN, phân loại iOS/Android tự động (v2.1.54)
- **Cố định mã QR bàn mang IP nội bộ LAN (`table_qr`)**:
  - Loại bỏ hoàn toàn việc nhúng link Cloudflare `public_base_url` trực tiếp vào ảnh QR bàn tại endpoint `/qr/table/<table_id>.png`.
  - Tự động phát hiện địa chỉ IP mạng nội bộ của máy chủ qua `get_local_lan_ip()` (kể cả khi mở trang admin từ `localhost` hoặc từ Cloudflare).
  - Ảnh QR tại bàn luôn luôn mang URL IP nội bộ (ví dụ `http://192.168.x.x:8004/replay/camX`).
- **Luồng hoạt động phân loại thiết bị**:
  - Khi khách quét QR trên bàn, kết nối được gửi về máy chủ LAN nội bộ trước.
  - Máy chủ kiểm tra User-Agent tại `replay_cam`:
    + Thiết bị **iOS** (iPhone / iPad): Tự động chuyển hướng (302) sang Cloudflare HTTPS (`public_base_url`) để mở khóa Web Share API lưu 1 chạm vào Ảnh (Photos).
    + Thiết bị **Android / Khác**: **Giữ nguyên ở mạng LAN nội bộ HTTP**, tải nhanh, xem mượt, không tốn băng thông internet và không phụ thuộc Cloudflare.
- Tịnh tiến phiên bản lên **2.1.54**.
