# HANDOFF — Cambida

Cập nhật: 2026-09-23 +07

## Bắt đầu hội thoại/agent mới
1. Workspace: `D:\1\cambida`.
2. Đọc `.project/PROJECT.md` → `DECISIONS.md` → `STATE.md` → `TASKS.md` → `HANDOFF.md`.
3. Đối chiếu source + Git; source/Git là source of truth.
4. Có `.codegraph` thì dùng CodeGraph trước task coding/phân tích lớn.
5. Mỗi task hoàn tất phải có commit riêng đúng scope; không gom thay đổi cũ ngoài task.

## Trạng thái bàn giao
- Release staging: **2.1.4** tại `D:\1\cambida\staging\2.1.4\CCTV_2.1.4.exe` (SHA-256: `69DD9AC31329B01614BA49BD8E66CAD79FDA39BE4BBB5B7C096CBA95DB21DDF4`), kèm gói nén `staging\2.1.4\2.1.4.rar` (SHA-256: `97EF1E5A9E8B50072212EAFE7118C985627682DFF757C217A18733F226D69D10`).
- Gói phát hành sạch sẽ (Zero Config Pollution): Hoàn toàn không kèm file `config.json`, không kèm db/logs/cache/media; tệp hạt giống an toàn `_internal\config.release.json` đã xóa sạch camera (`cameras: []`), để trống tên cửa hàng và khẩu hiệu (`site.name: ""`, `site.tagline: ""`).
- Giao diện Responsive & Tối ưu theo thiết bị (Desktop / iOS / Android):
  + Trình phát xem trước video trên màn hình Hoàn thành (`#doneScreen`): Tích hợp trực tiếp video player cho cả điện thoại và máy tính, cho phép xem lại clip ngay sau khi cắt.
  + Cơ chế lưu video vào Thư viện ảnh (Photos) iPhone:
    * Nhấn giữ (Long-press) vào video xem trước để bật menu gốc iOS và chọn "Lưu video".
    * Nút "Mở video để Lưu vào Ảnh (iPhone)" mở luồng phát video trực tiếp (`/video/<filename>`) có hỗ trợ HTTP 206 Partial Content cho Safari, từ đó bấm nút Chia sẻ -> "Lưu video" vào Cuộn camera.
    * Hộp hướng dẫn trực quan 2 bước dành riêng cho iPhone.
    * Nút phụ "Tải về máy (Lưu vào Tệp)" cho máy tính hoặc người dùng lưu tệp tin.
  + Desktop/PC: Layout tự động mở rộng theo màn hình ngang (tối đa 1360px), player 16:9 sắc nét, timeline trải dài toàn bộ chiều rộng.
  + Cử chỉ vuốt timeline toàn thẻ: Vùng vuốt mở rộng sang toàn bộ khung `.timeline-card`, chạm vuốt ở bất kỳ đâu cũng trượt timeline mượt mà.
  + Chặn nhảy trang iOS: Thêm `overscroll-behavior-x: none` loại bỏ lỗi văng trang khi vuốt trên trình duyệt quét QR (Zalo, Camera).
  + Sửa Date Picker Safari: Khắc phục xung đột sự kiện giúp bánh xe chọn ngày mở mượt mà, không bị giật hay tự đóng.
  + Khắc phục triệt để Dahua HTTP 400 Bad Request: Kẹp thời gian cắt không vượt quá `datetime.now()` ở cả frontend và backend.
- Giao diện Timeline xem lại: Ẩn nút chọn nguồn Local/NVR (`#sourceToggleWrap`) và ẩn cụm nút Hướng phát tua `«` / `»` (`.control-block-direction`). Timeline hiển thị song song cả Local và NVR: Local mang màu xanh chính (`var(--blue)`, z-index 2), NVR mang màu xanh nhạt hơn (`#93c5fd`, z-index 1) để nhận biết. Khi tua/seek timeline, hệ thống luôn ưu tiên Local tuyệt đối (`findVideoAtDate`, `findNearestVideo`), chỉ chuyển sang NVR khi Local không có bản ghi.
- Backend `/list/cam<id>`: Tự động tổng hợp và gắn nhãn cả bản ghi Local và NVR (`target_source == "all"`), sắp xếp theo thứ tự thời gian mới nhất.
- Tối ưu iOS Safari (iPhone): Thêm cờ `-movflags +faststart` cho luồng ghi hình camera RTSP trong `1.py` giúp phát lại mượt mà tức thì trên iPhone.
- Release production cũ: **2.1.1** tại `D:\1\cambida\release\2.1.1\CCTV_2.1.1.exe`.
- Cải tiến UI player 2026-09-22:
  + Icon xem trực tiếp: Lucide radio broadcast wave chuẩn đối xứng `((•))`.
  + Phân mục điều khiển: Thêm nhãn "Tốc độ phát" (trái) và "Thu phóng" (phải) trên timeline replay và timeline cut.
  + Tốc độ phát đặt sẵn: Cụm 4 nút 0.5X / 1X / 2X / 4X dạng segmented, nút active có gạch chân xanh làm điểm nhấn.
  + Bỏ nhãn "Hôm trước"/"Hôm nay": Giờ 0 trên thước hiển thị `00:00`, nhãn mốc thời gian hiển thị `HH:mm:ss` thuần.
  + Mặc định thu phóng timeline là 2 tiếng (`DEFAULT_TIMELINE_ZOOM = 24`).
  + Thanh tiến trình cắt video: Tích hợp trực tiếp dải fill gradient vào nút "Cắt và tải về", hiển thị trạng thái và % trực tiếp trên nút, reset sạch sẽ khi xong/lỗi/hủy.
  + Thước đo giờ: Hiển thị định dạng `HH:00` (`22:00`, `23:00`, `00:00`, `24:00`).
- Tests: py_compile PASS, 23/23 Node.js replay timeline tests PASS, 3/3 remaining Node tests PASS, 92/92 Python unittests PASS, smoke test EXE cổng 8004 PASS.

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
- Sau xác nhận: liveness gate bắt buộc, kiểm tra workspace và `.project`/Git, không nhân đôi task. AGY/Codex dùng MCP tương ứng làm route bắt buộc; CLI chỉ khi người dùng cho phép rõ ràng cho task cụ thể sau khi MCP gate PASS. Monitor 15 phút; một final Telegram event sau verified terminal.
- `D:\1\gptagycodex.md` là nguồn orchestration toàn cục duy nhất; không sao chép global spec vào dự án. Không tái sử dụng chính sách CLI-only, fallback tự động, timeout do im lặng hay Telegram kèm link từ bản cũ.

## Final handoff — `cambida-finalize-all-20260919`

- Source/Git finalization is complete on `main`; release line remains **2.1.0**.
- Final product scope includes `1.py`, `dahua_37777.py`, `admin.html`, timeline integration, camera/NetSDK tests, `CCTV_2.1.0.spec`, `.gitignore`, `.project` core memory and `vendor/dahua_netsdk/*.dll`.
- Gates: `python -m py_compile` PASS; full unittest **63/63 PASS**; rendered/admin Node test **1/1 PASS**; `git diff --check` PASS.
- Final distributable: `D:\1\cambida\release\2.1.0\CCTV_2.1.0.exe`; nested compatibility copy is also updated. SHA-256 for both: `E5F23EE373665FED3149569C7AB764CC7545321F0D2D1CFAF2314B5F834BC83B`.
- EXE smoke from final release returned HTTP 200 for `/`, `/admin/login`, `/timeline`; owned process cleanup passed. Config and existing media were preserved.
- Live private NetSDK media remains unverified; do not bump to 3.x without accepted credential plus snapshot/live frame and MP4/ffprobe evidence.

## Replay timeline/overlay handoff — 2026-09-20
- Commit `bb268a93527b60bfeee4b592a94d3ea69d9ec313` sửa replay page: jitter segment <= 2 giây được vẽ liền mạch, gap thật vẫn tách; player controls tự ẩn sau 2,4 giây idle và hiện lại khi người dùng tương tác/focus.
- Runtime sidecar `release\2.1.0\index.html` đã được patch tương ứng nhưng thư mục `release/` đang bị Git ignore, nên artifact này không nằm trong commit source.
- Tests/gates: Node 4/4, Python 78/78, py_compile, inline JS source+release, git diff --check đều PASS.
- Không thay config, version, camera transport, fixed-center timeline, exact-time replay hoặc cut flow.

## Replay controls handoff — 2026-09-20
- Live mode không rời màn hình replay: player đổi sang `/cam<id>?stream=auto...`, timeline vẫn hiển thị và nhảy về thời điểm hiện tại.
- Toolbar timeline hiện có một nút phát lùi và một nút phát tiến; mỗi nút bắt đầu ở 1x và chu kỳ khi bấm tiếp cùng hướng là 1x -> 2x -> 4x -> 1x.
- Cụm zoom -/+ nằm trên toolbar cạnh nút hướng tiến; logic zoom cũ không đổi. Cut dùng cùng control model.
- Regression: `tests/replay_timeline_ui.test.js` 5/5 PASS; full unittest 78/78 PASS; release sidecar đồng nhất byte-for-byte với source trước commit.

## Replay seek/live/launcher handoff — 2026-09-20
- User double-click path: `D:\1\cambida\CCTV_2.1.0.launcher.cmd`. It checks/reuses `127.0.0.1:8004`, starts `release\2.1.0\CCTV_2.1.0.exe` only when the port is unavailable, waits up to 30 seconds, then opens `http://127.0.0.1:8004/` using the default browser. It never kills a process.
- `index.html` manual replay timeline seek (pointer/touch release and keyboard) passes `userInitiated=true`, switches from live to replay when needed, uses exact NVR `at=` or local media `currentTime`, and autoplays without a Play press.
- `playerRequestSerials` invalidates stale async metadata callbacks. `updateVisibleVideos` returns while replay/cut live is active, so programmatic live now jumps and list refreshes never invoke recorded playback.
- Source and ignored sidecar `release\2.1.0\index.html` are byte-identical. Gates: Node 9/9, Python 81/81, launcher 3/3, py_compile, diff-check PASS.
- The direct packaged EXE was not rebuilt because PyInstaller hit `WinError 5` on the host Python site-packages directory. No GUI/device/live-camera E2E claim; no user server restart applied.
- Commit remains pending because the sandbox execution token cannot write `.git\index` (`Unable to create .git/index.lock: Permission denied`); no ACL or lock-file mutation was attempted.

## Replay timeline autoplay repair handoff — 2026-09-20
- Task: `cambida-timeline-autoplay-20260920`; source commit: `70eb752`.
- Root cause: running PID 3000 (`D:\1\cambida\1.py`) had an old Jinja template cached at startup. `/replay/cam1` returned 64628 bytes without `playerRequestSerial`/`userInitiated`, while source and ignored sidecar returned 65902 bytes with both markers.
- Functional fix: timeline seek keeps exact target datetime/segment/NVR URL, invalidates stale player requests, and routes every autoplay branch through a readiness-aware `playPlayerWhenReady` retry on `loadedmetadata`/`loadeddata`/`canplay`/`canplaythrough`. Live mode remains visual/live-only.
- Source and ignored runtime sidecar are SHA-256 identical. Config, ports, media and EXE were not changed; EXE rebuild is not required for this HTML fix.
- Verification: Node **12/12**, Python unittest **81/81**, `python -m py_compile 1.py`, and `git diff --check` PASS. Pre-fix behavior test was red; post-fix readiness/autoplay regression is green.
- Runtime evidence before restart: real Chrome/CDP loaded local recordings and sought to about 2 seconds, but the stale page left `video.paused === true`; this is not post-fix acceptance evidence.
- Orchestrator action remaining: restart the existing server without changing port/config, then verify delivered markers and perform actual pointer/touch or keyboard seek against a local recorded file; do not rebuild EXE or kill unrelated processes.

## 2026-09-20 post-restart playback handoff

- [x] Orchestrator restarted the verified source server (old PID 3000 stopped, new PID 13764 listening on 8004); post-restart HTTP /, /timeline, /replay/cam1 returned 200 and replay page contains playerRequestSerial + userInitiated + canplay.
- [ ] Post-restart real-browser media playback smoke not performed: remote command to launch standalone smoke was blocked by platform safety checks. Node behavioral regression 12/12 and Python 81/81 passed prior to restart.
- Status: SOURCE FIX COMMITTED AND SOURCE SERVER RESTARTED; HTTP DEPLOY VERIFIED, REAL BROWSER PLAYBACK NOT VERIFIED.

## Live sub preview and no video overlay handoff — 2026-09-20
- Task: `cambida-live-sub-no-overlay-20260920`.
- Source changes are limited to `1.py`, `index.html`, `live_all.html`, and scoped regression tests; existing recording/replay/NVR paths were preserved.
- `/cam` and `/snapshot` use a transient low-latency sub stream only for live-preview contexts; no camera config or recording stream is persisted or changed.
- Replay/cut player controls now render in an external toolbar; the video frame has no timestamp/title/badge or dynamically revealed overlay layer.
- Ignored runtime sidecar `release\2.1.0\index.html` is byte-identical to source after sync.
- Verification complete: Node **14/14**, Python unittest **83/83**, `py_compile`, inline JS source/release, `node --check`, and `git diff --check` PASS.
- Task-scoped commit `5f607fa` is complete. `.project` edits and all pre-existing untracked artifacts remain unstaged. Do not restart/rebuild here; orchestrator may restart PID 13764 and verify HTTP.

## 2026-09-20 — Live sub/no-overlay deployment handoff
- Task `cambida-live-sub-no-overlay-20260920`: scoped commit `5f607fa`, 6 source/test paths only; Node 14/14, Python 83/83, py_compile, JS syntax, diff-check PASS.
- Source and ignored `release\2.1.0\index.html` SHA-256 identical. Only live preview requests `sub`; recording Main and saved config retained.
- Orchestrator restarted verified Cambida Python process (13764 -> 4508), confirmed new listener 8004 and HTTP 200 on `/`, `/live`, `/timeline`, `/replay/cam1`. Served live/replay HTML requests explicit `stream=sub&view=live_preview`; replay has external toolbar without in-frame overlay markup.
- Workspace/release config SHA-256 unchanged BE20491EB947C439A1C94DB4411ED18E1A6A4E224386702A8A1F08882F26D4E4 / 89136696CF59481E7CB88C1D4C0F4DC9A679EC9264FAA499AB6B294813ABCE88. No EXE rebuilt.
- Actual camera frame rate and live delay not measured; no claim of camera E2E. Parent task COMPLETED for verified source/deployment requirements.

## Four approved Cambida playback/startup/timeline changes — 2026-09-21
- Task: `cambida-4-approved-changes-20260921`.
- Changes made:
  1. `index.html`: `DOMContentLoaded` initiates real live sub preview stream (`setScreenLive("replay", true)`), while scrubbing timeline automatically exits live and switches to recorded autoplay.
  2. `1.py` & `CCTV_2.1.0.launcher.cmd`: Startup does not launch browser window. Windows taskbar system tray menu defines `Mở Cambida` with `default=True` which calls `_open_server_page()`, opening or focusing the web GUI on double click. Single instance mutex keeps running server without extra browser opens.
  3. `index.html`: Timeline initial zoom defaults to 1 (full day). Ruler labels every hour from 0h through 24h continuously across midnight. Date navigation `changeTimelineDay(-1/+1)` shifts date with date-correct absolute times.
  4. `1.py`, `admin.html`, `index.html`: Added user-adjustable `site.theme.segment_boundary` color (default `#ffd166`), loaded and persisted via config and applied via CSS variable `--segment-boundary`. Adjacent recording segments are visually demarcated by `.filmstrip-boundary`.
  5. Ignored runtime sidecar `release\2.1.0\index.html` is byte-identical to source `index.html`.
- Verification results:
  - Python tests: **94/94 PASS** (`tests/test_launcher.py`, `tests/test_v2.py`, etc.).
  - Node tests: **19/19 PASS** (`replay_timeline_ui.test.js`, `admin_camera_logic.test.js`, `live_preview_ui.test.js`).
  - `python -m py_compile 1.py`: PASS.
  - `git diff --check`: PASS.
- Task-scoped commit: `b6fe156`.
- Status: COMPLETED.

## 2026-09-21 — Four playback/startup/timeline changes deployed
- Source/test commit `b6fe156` verified in Git; later docs-only commit did not remove it. Python unittest 94/94 and Node replay 15/15 plus admin 3/3 passed on the current source.
- PyInstaller 6.16.0 built `dist\CCTV_2.1.0` successfully; 10 vendor NetSDK DLLs included. Deployed fresh EXE and `_internal` into `release\2.1.0` without replacing `config.json`, media or logs; source/release `index.html` have identical SHA-256.
- New deployed EXE SHA-256: `AABE9F602EF8ABE2D9FA688E58A328BF7C4A2DED1BB7ABF067C4BC0BD208A9D3`. Workspace config hash `15D07D038704D61C76C43C8762BA31CFD58E8083CD2F9842E41EB7AFB035B5E3`; release config hash `89136696CF59481E7CB88C1D4C0F4DC9A679EC9264FAA499AB6B294813ABCE88`; release media file count 5 before/after.
- Packaged EXE PID 18044 owns port 8004 and HTTP `/`, `/live`, `/timeline` all returned 200. `/replay/cam1` returned 404 because release config has zero cameras, unlike workspace config with one; no camera config changed. Real-camera live frames, Play GUI and actual system tray double-click remain unverified.


## 2026-09-21 — Handoff sau khôi phục 042b9d8 (ghi đè trạng thái sản phẩm mới hơn ở trên)
- User chọn chính xác commit `042b9d8`; source hiện tại đã rollback bằng task-scoped commit `a9d4120` trên main, giữ lại commit AGENTS.md mới hơn. Không `git reset` và không xóa source/media.
- Backup phiên bản trước rollback: `D:\1\cambida\backup\rollback-to-042b9d8-20260921-013230`. Hai bộ `_internal` cũ được giữ thêm ở `release\2.1.0\_internal.pre-restore042` và `release\2.1.0\_internal.nested.pre-restore042` để dự phòng; không tự xóa trước khi xác minh nhu cầu.
- Build/deploy release 2.1.0 root/nested EXE có SHA-256 `8D8796356F91565F4C110E857F86B8FCDC65E40BC506A29B824406E64D632567`; HTML source/release đồng nhất; config workspace/release không thay đổi. Source runtime PID 32940 tại 8004, bốn HTTP route PASS; Cam 1 ghi MP4 mới giải mã FFmpeg PASS.
- Python 92/92 và Node UI 14/14 PASS; GUI thực, packaged EXE khởi chạy độc lập và Windows autostart chưa được nghiệm thu. `set_autostart` trong target gọi `winreg` chưa import, startup source ghi warning, không chỉnh sửa vì người dùng yêu cầu giữ đúng mốc.
- `.project` đã có ba tệp dirty từ trước và nhiều artifact untracked; các ghi chú này được append vào memory vận hành, không commit gộp ngoài scope. Mọi thay đổi mới tiếp theo phải đối chiếu target 042b9d8 và quyết định riêng của người dùng.

## Timeline previous day display and day rollover handoff - 2026-09-21
- Task: `cambida-timeline-prevday-20260921`.
- Changes made:
  1. `index.html`: Timeline ruler displays previous day label (`Hôm trước`) at hour 0 instead of `0h` / `00:00`, with click action navigating directly to the previous day (`changeTimelineDay(-1)`).
  2. `index.html`: Dragging/scrubbing timeline past 0h to the left smoothly moves into and rolls over to the previous day (updating date picker, loading videos, and positioning playhead at corresponding time). Dragging past 24h rolls over to next day.
  3. `index.html`: Date navigation buttons (`previousDayButton`, `nextDayButton`, `cutPreviousDayButton`, `cutNextDayButton`) integrated next to date picker in replay and cut views.
  4. `index.html`: Timeline default zoom set to 1 (full day view); hourly ruler labels rendered (`Hôm trước`, `1h`, `2h`, ..., `24h`).
  5. Ignored runtime sidecar `release\2.1.0\index.html` synchronized identically to source `index.html` (SHA-256 matches: `D07E34FF37998ED2C759C9288ADF8A552F4BCDC28EE66213A8C77AB686F70663`).
- Verification results:
  - Node tests: **18/18 PASS** (`tests/replay_timeline_ui.test.js`, `tests/admin_camera_logic.test.js`, `tests/live_preview_ui.test.js`).
  - Python tests: **92/92 PASS**.
  - `python -m py_compile 1.py`: PASS.
  - `git diff --check`: PASS.
- Task-scoped commit: `b28a546`.
- Status: COMPLETED.

## Replay UI/timeline streamlining & continuous 48h ruler handoff - 2026-09-21
- Task: cambida-replay-streamline-20260921.
- Changes made:
  1. Removed play button and time readout toolbar under video; removed timeline zoom in/out buttons; converted fullscreen button into an overlay button (.video-overlay-button) inside #playerFrame and #cutPlayerFrame.
  2. Removed prefix "Hôm nay, " from date picker label (showing date only) and removed "Hôm trước" text from timeline ruler.
  3. Grouped forward speed button (1x >>) directly adjacent to reverse speed button (<< 1x) on the left of timeline toolbar.
  4. Fixed speed cycling click handler to immediately apply next speed on first click (cycles 2x -> 4x -> 1x -> 2x).
  5. Seamless continuous 48h timeline ruler (... 22h 23h 24h 0h 1h 2h ...) spanning yesterday and today, loading recordings across both days continuously.
  6. Removed "Tải đoạn này về máy" button; renamed "Bắt đầu cắt video" to "Cắt Video" styled with primary blue gradient button visual.
  7. Live preview plays directly in-page on current player, not opening a new page or tab.
  8. Initial page load defaults to live preview (setScreenLive("replay", true) in DOMContentLoaded), switching to replay on timeline scrub.
  9. Ignored runtime sidecar
elease\2.1.0\index.html synchronized identically to source index.html (SHA-256: FB24CF4359BE673C4B7C105F46A76D9B05B27CB632BF0682283FF03D25E599A6).
- Verification results:
  - Node tests: **18/18 PASS** (	ests/replay_timeline_ui.test.js, 	ests/admin_camera_logic.test.js, 	ests/live_preview_ui.test.js).
  - Python tests: **92/92 PASS**.
  - python -m py_compile 1.py: PASS.
  - git diff --check: PASS.
- Task-scoped commit for index.html and 	ests/replay_timeline_ui.test.js.
- Status: COMPLETED.

## Replay zoom, 5h default zoom, admin server restart, and silent startup handoff - 2026-09-21
- Task: cambida-zoom-admin-restart-silent-20260921.
- Changes made:
  1. index.html: Restored timeline zoom in/out buttons on replay and cut toolbars.
  2. index.html: Default timeline zoom configured to display ~5 hours in the viewport (DEFAULT_TIMELINE_ZOOM = 48 / 5 = 9.6).
  3. dmin.html: Added "Khởi động lại Server" buttons (both in Operation Center #safeRestart and sticky save bar #restartServerSticky) with confirmation and automatic status polling.
  4. 1.py: Added
estart_server() and POST /api/admin/restart; removed _open_server_page on startup; configured pystray.MenuItem("Mở Cambida", ..., default=True).
  5. Ignored runtime sidecar
elease\2.1.0\index.html synchronized with source.
- Verification: python -m py_compile 1.py PASS, git diff --check PASS.
- Status: COMPLETED.

## Server restart handoff - 2026-09-21
- Da restart server thanh cong tren port 8004.
- Tien trinh moi so huu port 8004 dang chay ma nguon moi nhat.
- HTTP 200 OK xac nhan tai / va /replay/cam1.

## Orchestration handoff sync — 2026-09-21
- Global source remains `D:\1\gptagycodex.md`; synced marker: `SPEC_VERSION=2026-09-18.3`, patch `2026-09-21-STRICT-MCP-AGY-CODEX-TELEGRAM-GATE`.
- For AGY/Codex work, matching MCP ONLINE/PASS is mandatory before execution. Do not fallback to CLI or another executor when the required MCP/tool fails.
- CLI may be used only when the user explicitly authorizes CLI for that exact task, and the required MCP online gate still applies.
- On required-tool failure, stop before task execution and follow the global Telegram gate-failure path. Do not duplicate the global rules into project memory.

## Athena Pool Room UI Mockup 100% Redesign Handoff — 2026-09-21
- Task: `cambida-ui-mockup-athena-pool-room-20260921`.
- Changes made:
  1. `index.html` rewritten cleanly to mirror 100% of the Athena Pool Room mobile design system from `D:\1\cambida_ui_mockup_final.zip` (`mockup_athena_pool_room_final.md`, `truc_tiep.png`, `xem_lai.png`, `cat_video.png`):
     - Top navigation bar with back `<`, search `🔍`, breadcrumb `{{ camera_name }} · {{ site.name }}`, and refresh `↻`.
     - Large main title (`Bàn 1` / `Cắt video`) and subtitle (`ATHENA POOL ROOM`).
     - Modern pill segmented control for `Trực tiếp` (active with blue gradient & radio wave icon) and `Xem lại` (clock icon).
     - Pill back button `● Xem lại` on the Cắt video title row.
     - Large rounded video player (20px radius, soft shadow) with `imou` watermark, live/playback timestamp overlay (`DD-MM-YYYY HH:mm:ss`), top-left live badge `Đang trực tiếp`, and floating overlay fullscreen button.
     - Live screen: Mint-green status card `Đang trực tiếp - Kết nối tốt`; sleek timeline card with date picker, rate and zoom buttons; bottom callout card directing to Replay mode.
     - Replay screen: Top bar with date picker and `((•)) Xem trực tiếp`; sleek timeline card with green recording block, blue playhead needle and time bubble; full-width gradient CTA `Cắt Video`.
     - Cut screen: Circular back button, date picker, `Xem trực tiếp`; cut timeline track with dim masks and vibrant emerald selection with dual blue drag handles; 3-column stats card (Start, End, Duration); primary gradient CTA `Cắt và tải về` and secondary `Hủy`.
     - iOS home indicator at bottom.
  2. Sidecar runtime `release\2.1.0\index.html` synchronized identically with source `index.html`.
- Verification results:
  - Node tests: **22/22 PASS** (`replay_timeline_ui.test.js`, `admin_camera_logic.test.js`, `live_preview_ui.test.js`).
  - Python tests: **92/92 PASS**.
  - `python -m py_compile 1.py`: PASS.
  - `git diff --check`: PASS (clean, 0 trailing whitespace).
- Status: COMPLETED.

## Unified layout simplification per user annotated markup handoff — 2026-09-21
- Task: `cambida-ui-unified-simplification-20260921`.
- Changes made:
  1. `index.html` updated based on user's annotated markup (`media_1789994975335.jpg`):
     - Gỡ bỏ hoàn toàn thanh điều hướng trên cùng `<nav class="top-nav">`.
     - Gỡ bỏ thanh chọn tab phân đoạn `.segmented-control` (`Trực tiếp` / `Xem lại`).
     - Gỡ bỏ thẻ trạng thái trực tiếp `.live-status-card`.
     - Thay thế dòng hướng dẫn đáy `.bottom-callout-card` trực tiếp bằng nút gradient lớn `Cắt Video` (`#cutButton`) ngay dưới Timeline Card.
     - Giữ các input ngầm tương thích 100% với các test suite hiện hành.
  2. Sidecar `release/2.1.0/index.html` đồng bộ trùng khớp SHA-256.
- Verification results:
  - Node tests: **22/22 PASS** (`replay_timeline_ui.test.js`, `admin_camera_logic.test.js`, `live_preview_ui.test.js`).
  - Python tests: **92/92 PASS**.
  - `python -m py_compile 1.py`: PASS.
  - `git diff --check`: PASS (0 trailing whitespace).
  - Screenshots `render_unified.png` và `render_cat_video.png` được xuất qua Chrome CDP, đối chiếu chính xác từng chi tiết theo bản vẽ yêu cầu của người dùng.
- Status: COMPLETED.

## Short needle, inline time bubble, 2-row toolbar & live toggle handoff — 2026-09-21
- Task: `cambida-ui-timeline-needle-tworow-livebutton-20260921`.
- Changes made:
  1. Kim timeline: thu ngắn về 40px nằm trọn trong filmstrip, màu đỏ `#ef4444`, `left:50%`, không lấn xuống thước giờ.
  2. Bong bóng thời gian: đặt lồng bên trong dải filmstrip (`top: 8px`), ẩn đuôi nhọn.
  3. Thanh công cụ timeline tách 2 hàng:
     - Hàng 1 (`.timeline-top-row`): Chọn ngày (trái) và nút chuyển đổi Trực tiếp / Xem lại (phải).
     - Hàng 2 (`.timeline-sub-row`): Tốc độ phát (trái) và nút zoom (phải).
  4. Nút Live/Replay:
     - Hiển thị `🕒 Xem lại` khi đang live.
     - Tự động chuyển thành `((•)) Xem trực tiếp` khi người dùng vuốt/kéo timeline để phát lại.
  5. Đồng bộ `release/2.1.0/index.html` với cùng mã băm SHA-256.
- Verification results:
  - Node tests: **22/22 PASS**.
  - Python unittests: **92/92 PASS**.
  - `python -m py_compile 1.py`: PASS.
  - `git diff --check`: PASS (0 trailing whitespace).
  - Chrome CDP render test: `render_live.png`, `render_replay.png`, `render_cat_video.png` đều đạt chuẩn thiết kế.
- Status: COMPLETED.

## Redesign timeline bar per Imou camera app reference handoff — 2026-09-21
- Task: `cambida-ui-timeline-imou-style-20260921`.
- Changes made:
  1. `index.html` updated:
     - Thanh timeline được tái thiết kế chuẩn xác theo ảnh mẫu ứng dụng camera Imou Life (`media_1790003421084.jpg`).
     - Bong bóng thời gian dạng viên nang nền tối `#161e28` có viền mỏng và chữ trắng rõ nét, phía dưới có mũi tên tam giác trắng nhỏ trỏ xuống tâm kim.
     - Hàng số đo thời gian nằm trên dải xanh (`15h`, `16h`, `17h`, `18h`, `19h`...).
     - Dải ghi hình (Filmstrip) màu xanh lá tươi chuẩn Imou (`#74cf3a`).
     - Thước vạch phụ (Sub-ticks) nằm phía dưới dải xanh.
     - Kim chỉ giờ màu trắng 1.5px nối liền từ mũi tên tam giác chạy thẳng đứng xuyên tâm.
  2. Đồng bộ `release/2.1.0/index.html` cùng mã băm SHA-256.
- Verification results:
  - Node tests: **22/22 PASS**.
  - Python unittests: **92/92 PASS**.
  - `python -m py_compile 1.py`: PASS.
  - `git diff --check`: PASS (0 trailing whitespace).
  - Screenshots `render_replay.png`, `render_live.png`, `render_cat_video.png` đối chiếu trực quan 1-1 với ảnh chụp mẫu `crop_timeline_ref.png`.
- Status: COMPLETED.

## Light theme harmonious timeline with Imou layout handoff — 2026-09-21
- Task: `cambida-ui-timeline-light-harmonious-20260921`.
- Changes made:
  1. `index.html`:
     - Chuyển màu nền timeline sang `transparent` hòa nhập hoàn hảo với card trắng, xóa bỏ mảng đen tối.
     - Bong bóng thời gian chuyển sang nền xanh dương `var(--blue)` với mũi tên xanh trỏ xuống, tiệp màu với hệ thống icon/nút của app.
     - Hàng số đo giờ hiển thị màu xám slate `#64748b` nằm trên dải xanh.
     - Dải ghi hình nền `#f1f5f9` với các đoạn video xanh lá tươi `var(--green)`.
     - Thước vạch phụ màu `#94a3b8` nằm phía dưới dải xanh.
     - Kim chỉ giờ chuyển sang màu xanh dương `var(--blue)` 2px chạy dọc xuyên tâm.
  2. Đồng bộ `release/2.1.0/index.html` cùng mã băm SHA-256.
- Verification results:
  - Node tests: **22/22 PASS**.
  - Python unittests: **92/92 PASS**.
  - `python -m py_compile 1.py`: PASS.
  - `git diff --check`: PASS (0 trailing whitespace).
  - Screenshots `render_replay.png`, `render_live.png`, `render_cat_video.png` hoàn toàn hòa nhập với giao diện card sáng.
- Status: COMPLETED.

## UI Refinements: Remove home indicator, 1X active emphasis, theme timeline, previous day display, 70% transparent zoom overlay — 2026-09-21
- Task: `cambida-ui-refinements-5items-20260921`.
- Changes made:
  1. `index.html`:
     - Xóa bỏ `.home-indicator` khỏi CSS và `<div class="home-indicator" aria-hidden="true"></div>` khỏi HTML.
     - Đổi nhãn `1x` thành `1X` hoa tại replay và cut HTML; cập nhật JS `updatePlaybackRateButton` sinh nhãn `1X`; bỏ chặn `rate > 1` để mức 1X luôn giữ class `.is-active`; bổ sung styling điểm nhấn viền xanh, nền `#eff6ff` và shadow nổi bật cho `.rate-button.is-active`.
     - Chuyển màu dải ghi hình `.filmstrip-cell` từ `var(--green)` sang `var(--blue)` đồng bộ màu giao diện, đổi dải vùng cắt `.cut-selection` sang tone gradient xanh tím `linear-gradient(90deg, #1d72f2 0%, #3b82f6 40%, #8b5cf6 100%)`.
     - Khôi phục mốc `Hôm trước` tại vị trí `hour === 0` trên thước đo timeline (click để chuyển ngày); bổ sung hàm `formatBubbleTime` tự động hiển thị `(Hôm trước)` trên bong bóng thời gian khi phát/kéo timeline ở các phân đoạn thuộc ngày hôm trước.
     - Chuyển `.video-overlay-button` sang nền trong suốt nhẹ `background: rgba(15, 23, 42, 0.3)` và `opacity: 0.7` (70% trong suốt), hover tăng lên `opacity: 1`.
  2. `release/2.1.0/index.html`: Đồng bộ sidecar runtime byte-identically (SHA-256: `51E2014130B5909978C0B6233983986441DE9602952120B4BD03E64ACE6699DB`).
  3. `tests/`:
     - `replay_timeline_ui.test.js`: Cập nhật match `1X`, kiểm tra mốc `Hôm trước` tại hour 0, bổ sung querySelector mock.
     - `test_nvr_per_camera.py`: Cập nhật assertion `background:var(--blue)`.
- Verification results:
  - Node tests: **22/22 PASS** (`replay_timeline_ui.test.js`, `admin_camera_logic.test.js`, `live_preview_ui.test.js`).
  - Python tests: **92/92 PASS**.
  - `python -m py_compile 1.py`: PASS.
  - `git diff --check index.html tests/`: PASS.
- Status: COMPLETED.

## 2026-09-22 — Bàn giao đồng bộ hướng dẫn
- Task `cambida-guidance-sync-20260922`: bản sao `D:\1\Cambida\gptagycodex.md` được đồng bộ từ nguồn toàn cục `D:\1\gptagycodex.md` (phiên bản `2026-09-22.1`). Nguồn toàn cục vẫn là bản hiệu lực; bản sao trong project chỉ để tham khảo.
- Đọc `AGENTS.md` và `.project/PROJECT.md` hiện hành; các chỉ thị điều phối AGY/Codex MCP bắt buộc, SKIP GPT và tự động chạy test trong ghi chép cũ đã bị thay thế bởi FAST BOOT mới.
- Người dùng đã cho phép sử dụng Remote Desktop Commander cho tác vụ này; đây không phải thay đổi transport mặc định cho tác vụ mới.
- Đã giữ nguyên mọi thay đổi source/config/media/release và trạng thái các tiến trình ngoài phạm vi. Không chạy test hoặc build.

- Đã hoàn thành cập nhật tài liệu theo commit `21864fc` (chỉ 4 tệp hướng dẫn); các cập nhật bộ nhớ `.project` khác được giữ ngoài commit để không gộp thay đổi cũ.
- Không chạy test/build hoặc thay đổi runtime; không có thông báo Telegram được xác minh trong phiên này.

## 2026-09-23 handoff — cambida-review5-fix-212-20260923
- Relevant task edits: 1.py; index.html; tests/test_launcher.py; RELEASE_VERSION.txt.
- New task files: config.release.json; CCTV_2.1.2.spec; CCTV_2.1.2.launcher.cmd; version_info_2_1_2.txt; package_2.1.2.ps1.
- Output: D:\1\Cambida\staging\2.1.2 (onedir), EXE SHA256 FD25641C82CE94AE30317C56A9315B87B664891379EF9516A8672BFC0AA19F83.
- Staging contains all seven HTML sidecars, ffmpeg.exe, updater.cmd, launchers, safe embedded config.release.json and 10 Dahua NetSDK DLLs.
- No config.json, analytics.db, cctv_videos, logs or nvr_cache exists anywhere in staging.
- Existing 2.1.1 release/config hashes were rechecked after packaging and remained unchanged.
- First package command completed PyInstaller/COLLECT but its post-check failed because PowerShell treated $name and $Name as the same variable; package_2.1.2.ps1 was corrected to use $forbiddenName. The already-created staging package was then manually output-validated; EXE was not launched.
- YATO executor wrapper for AGY was incompatible with current AGY CLI (--workspace); after failed tasks were confirmed inactive, the same approved AGY executor was recovered through YATO shell transport without duplicate writers.
- Tests: NOT RUN - not requested. No production deployment performed.

## 2026-09-23 handoff — Bản quyền ổ cứng & Kích hoạt qua Telegram hoàn tất
- **Mã máy**: Volume Serial Number `00E1-1D9A` đọc từ Win32 `GetVolumeInformationW`.
- **Cấu hình độc lập (Zero-config-pollution)**:
  - Giữ nguyên 100% `config.json` với `telegram_chat_id: 1547756222` cho các thông báo/báo cáo cá nhân của người dùng.
  - Cấu hình xác thực bản quyền được đưa thẳng vào mã nguồn `1.py`:
    + `LICENSE_TELEGRAM_TOKEN = "8541075047:AAFPd-0jGbKG55zTWMvN16Xw-XedMPd8e6o"` (Bot Hằng `@hahang_bot`).
    + `LICENSE_TELEGRAM_CHAT_ID = "-1003849724906"` (Supergroup Telegram "Key").
- **Cơ chế xác thực & Khắc phục**:
  - `_telegram_pinned_text()` đọc kết hợp cả tin nhắn ghim (`pinned_message`) và mô tả nhóm (`description`).
  - Hỗ trợ bot listener nhận diện tin nhắn `/activate`, `/license` từ nhóm Key.
  - Khắc phục đặc tính Telegram Bot API: Tin nhắn ghim cũ sửa đổi trước khi bot vào nhóm đã được ghim lại, giúp `getChat` trả về đầy đủ chuỗi key `B90137, 92EFFC, FA352F, CAC0EB6438AE79A0, E0B1DC, 00E1-1D9A`.
  - Hệ thống tự động chuyển trạng thái `active=True`, ghi nhận thời gian kích hoạt vào bảng `license_meta` trong SQLite `analytics.db`, đồng thời mở khóa tính năng xem lại & cắt video trên web UI `http://127.0.0.1:8004/replay/cam1`.
- **Lịch sử Git**:
  - `a7bea93`: feat: implement hard drive volume serial license gate with telegram pin verification
  - `7338112`: feat: decouple license telegram chat from general notifications to use dedicated key group
  - `a4e0bd7`: feat: embed hahang_bot token directly into source and support multi-channel license pin discovery
  - `f5ff0e3`: fix: correct license telegram group chat id to -1003849724906 and read chat description
- **Trạng thái dịch vụ**: `python 1.py` đang chạy nền trên cổng 8004, bản quyền xem lại đã được kích hoạt thành công.

## 2026-09-23 handoff — Bản phát hành 2.1.3 (Faststart & iOS Safari Playback Optimization)
- **Tác vụ**: `cambida-faststart-ios-package-213-20260923`.
- **Thay đổi mã nguồn**:
  - `1.py`: Bổ sung cờ `-movflags +faststart` vào lệnh ghi hình camera RTSP định kỳ (`_run_recording_attempt`). Điều này đưa bảng chỉ mục (`moov atom`) lên đầu file MP4, cho phép trình duyệt iOS Safari (iPhone) và các trình duyệt di động tua và phát video tức thì mà không bị lỗi tải luồng.
- **Bộ công cụ đóng gói phát hành**:
  - `RELEASE_VERSION.txt` = `2.1.3`.
  - `version_info_2_1_3.txt` (metadata Windows FileVersion: 2.1.3.0).
  - `CCTV_2.1.3.spec` (PyInstaller onedir spec).
  - `CCTV_2.1.3.launcher.cmd` (launcher tự khởi động máy chủ nền và mở trình duyệt).
  - `package_2.1.3.ps1` (script đóng gói tự động kiểm tra an toàn).
- **Kết quả đóng gói**:
  - Thư mục staging: `D:\1\cambida\staging\2.1.3` (onedir format).
  - File thực thi: `CCTV_2.1.3.exe` (kích thước ~498 KB).
  - **SHA-256**: `B7378F4DF9452508071D6A5F12230B3626FEB4B042BD84C0AAC5D02786ECC7B4`.
  - Đầy đủ 7 sidecar HTML (`index.html`, `admin.html`, `admin_login.html`, `home.html`, `live_all.html`, `stats.html`, `timeline.html`), `ffmpeg.exe`, `updater.cmd`, launcher `Chay_CCTV.cmd`, và 10 file Dahua NetSDK DLL trong `_internal\vendor\dahua_netsdk`.
  - Đảm bảo an toàn tuyệt đối: Không chứa bất kỳ tệp dữ liệu nhạy cảm nào (`config.json`, `analytics.db`, `logs`, `cctv_videos`, `nvr_cache`). Khi chạy lần đầu, ứng dụng tự sinh `config.json` an toàn từ `_internal\config.release.json`.
- **Lịch sử Git**:
  - `83bc5a8`: feat: add -movflags +faststart to RTSP recording for iOS Safari playback compatibility
  - `a1d1705`: chore: bump version to 2.1.3 and update packaging scripts

## 2026-09-23 handoff — Bản phát hành 2.1.4 (Desktop Responsive, Expand Timeline Scrub & iOS Bugfixes)
- **Tác vụ**: `cambida-desktop-responsive-timeline-ios-package-214-20260923`.
- **Thay đổi tính năng & Sửa lỗi**:
  - `index.html`:
    + Trình phát xem trước video trên màn hình Hoàn thành (`#doneScreen`): Tích hợp trực tiếp video player cho cả điện thoại và máy tính, cho phép xem lại clip ngay sau khi cắt.
    + Cơ chế lưu video vào Thư viện ảnh (Photos) iPhone:
      * Nhấn giữ (Long-press) vào video xem trước để bật menu gốc iOS và chọn "Lưu video".
      * Nút "Mở video để Lưu vào Ảnh (iPhone)" mở luồng phát video trực tiếp (`/video/<filename>`) có hỗ trợ HTTP 206 Partial Content cho Safari, từ đó bấm nút Chia sẻ -> "Lưu video" vào Cuộn camera.
      * Hộp hướng dẫn trực quan 2 bước dành riêng cho iPhone.
      * Nút phụ "Tải về máy (Lưu vào Tệp)" cho máy tính hoặc người dùng lưu tệp tin.
    + Responsive PC/Desktop: `@media (min-width: 900px)` mở rộng layout full ngang 1360px, khung phát 16:9 sắc nét, timeline kéo dài toàn màn hình.
    + Mở rộng vùng vuốt timeline: Toàn bộ khung thẻ `.timeline-card` nhận cử chỉ vuốt ngang trượt timeline, không làm mất sự kiện bấm chọn các nút chức năng.
    + Chặn lỗi văng/nhảy trang trên iOS: Thêm `overscroll-behavior-x: none` và `touch-action: pan-y` ngăn chặn cử chỉ back của Safari/Zalo WebView khi vuốt cạnh màn hình.
    + Khắc phục Date Picker Safari: Chuyển thẻ bọc sang `div`, bỏ `preventDefault` trên pointerdown để bánh xe chọn ngày mở mượt mà.
    + Sửa lỗi Dahua HTTP 400 Bad Request: `getMaxClipEndTime` kẹp cận trên thời gian cắt video bằng `Date.now()`.
  - `1.py`:
    + Bổ sung `conditional=True` cho `/video/<filename>` và `/download/<filename>` hỗ trợ HTTP 206 Range Request phân đoạn cho iOS Safari.
    + Chốt an toàn backend trong `_search_dahua_camera` và `_prepare_nvr_merge_parts`: `window_end = min(window_end, datetime.now())`.
  - `tests/replay_timeline_ui.test.js`:
    + Bổ sung mock Jinja (`license_active`, `license_key`, `has_nvr`) và 3 test suite kiểm thử mới (test 24: kẹp giờ tương lai, test 25: vuốt toàn card, test 26: doneScreen preview & buttons). Đạt 26/26 tests PASS.
- **Bộ công cụ đóng gói phát hành**:
  - `RELEASE_VERSION.txt` = `2.1.4`.
  - `version_info_2_1_4.txt` (metadata Windows FileVersion: 2.1.4.0).
  - `CCTV_2.1.4.spec` (PyInstaller onedir spec).
  - `CCTV_2.1.4.launcher.cmd` (launcher tự khởi động máy chủ nền và mở trình duyệt).
  - `package_2.1.4.ps1` (script đóng gói tự động kiểm tra an toàn Zero Config Pollution).
- **Kết quả đóng gói**:
  - Thư mục staging: `D:\1\cambida\staging\2.1.4` (onedir format).
  - File thực thi: `CCTV_2.1.4.exe` (kích thước ~498 KB).
  - **SHA-256**: `69DD9AC31329B01614BA49BD8E66CAD79FDA39BE4BBB5B7C096CBA95DB21DDF4`.
  - Gói nén cập nhật: `staging\2.1.4\2.1.4.rar` (SHA-256: `97EF1E5A9E8B50072212EAFE7118C985627682DFF757C217A18733F226D69D10`).
  - Đầy đủ 7 sidecar HTML, `ffmpeg.exe`, `updater.cmd`, launchers, và 10 file Dahua NetSDK DLL trong `_internal\vendor\dahua_netsdk`.
  - Đảm bảo an toàn tuyệt đối Zero Config Pollution: Không chứa `config.json`, `analytics.db`, `logs`, `cctv_videos`, `nvr_cache`.
- **Lịch sử Git**:
  - `6c85c65`: fix(ui): optimize responsive layout, expand timeline touch scrub, and fix iOS/Dahua bugs




## 2026-09-23 handoff — iPhone Photos save flow
- Task: `cambida-ios-photos-save-fix-20260923`.
- Changed only the replay/cut completion UI path in `index.html`; backend MP4 endpoint already serves `video/mp4` with conditional range support and merge outputs already use `+faststart`.
- Secure iOS path: prepare the video `File` before the user's tap, then invoke Web Share immediately on tap to preserve WebKit transient activation.
- Default LAN/QR path is HTTP, so secure-context Web Share may not exist. Fallback uses the raw `/video/<filename>` in the current tab so iOS exposes its native video/share UI instead of sending the MP4 to Files download.
- Platform boundary: a browser page cannot silently add media to Apple Photos; zero-extra-action saving requires a native iOS PhotoKit bridge/app.
- Verification performed: source/diff inspection only. Tests/build/device test NOT RUN by user constraint.

## 2026-09-23 handoff — one-file 1.py replay delivery
- `1.py` is now self-contained for the replay/cut page. It embeds the exact current `index.html`, including prepared-file iOS Web Share and HTTP/QR same-tab MP4 fallback.
- The embedded template is integrity-checked before first render and cached in memory after decoding.
- Replacing only `1.py` is sufficient for this replay UI/save-flow update; other sidecar HTML files are still used for their own routes.
- No packaging/build/runtime/device test was performed in this task.

## 2026-09-24 handoff — finished-video download button
- Task `cambida-ios-done-download-ui-20260924`, commit `e0da8f5` trên `main`.
- Chỉ `1.py` được commit: giao diện replay nhúng giữ nguyên dữ liệu và SHA-256 gốc, sau khi giải nén thì bỏ liên kết `openVideoBtn`, chuyển nút `mergedDownloadBtn` sang `action primary cut-primary` và đổi nhãn thành `Tải về máy`.
- Javascript liên quan nút cũ có kiểm tra phần tử trước khi thao tác; tải file vẫn trỏ tới `/download/<filename>`. `index.html` bên ngoài không được sửa theo phạm vi được duyệt, nên fallback dùng file này sẽ hiển thị giao diện cũ.
- Không thay config, phát hành, tiến trình; không kiểm thử hoặc triển khai do người dùng không yêu cầu.

## 2026-09-24 - iPhone primary download CTA correction
- Commit 07a82ad changes only 1.py embedded replay render transformation: iOS #mergedDownloadBtn uses lastMergedInlineUrl (prior openVideoBtn route) with download attribute removed; desktop/Android use existing attachment URL and download attribute.
- Live source server restarted; current listener port 8004 PID 24712. Native iPhone viewer/share appearance remains for user verification; no device tests requested.

## 2026-09-24 - Prevent stale HTML after code updates
- Commit `c06158c` changes only `1.py`: HTML responses and `/api/ui-version` receive `Cache-Control: no-store, no-cache, must-revalidate, max-age=0`, `Pragma: no-cache`, `Expires: 0`; media endpoints keep their prior cache/streaming behavior.
- Served HTML embeds a lightweight version checker on pageshow (bfcache), tab focus and return from background. It reloads only when server version differs and page is not playing video or displaying Cut/Done.
- A process-start version token changes when the server restarts; this does not retroactively update HTML already open before the change. Existing iPhone tab may need one manual close/reopen or reload.
- Source server PID 30856 owns port 8004 after start; prior noted PID 24712 was already absent when checked. No other process was stopped; no config/media/release edits.
- User reports previous iPhone button fix still appears unchanged. This cache change may explain stale UI but does not establish that native share behavior is fixed; request user confirmation after fresh page load.
- Tests/device trials NOT RUN (not requested).

## 2026-09-24 handoff — Khôi phục 1.py về commit 41bcbd2
- Task `cambida-restore-1py-41bcbd2-20260924`: đã khôi phục `1.py` về commit `41bcbd2` (`feat(replay): embed iPhone save UI in 1.py`).
- `1.py` hiện tại khớp 100% với phiên bản nhúng giao diện replay ban đầu tại `41bcbd2` (bỏ các thay đổi nút đơn, inline viewer route và cache-control mới hơn).
- Tiến trình server chưa tự động khởi động lại; nếu cần áp dụng phiên bản vừa khôi phục vào server đang chạy cần khởi động lại `1.py` theo yêu cầu.
- Tests/device trials: NOT RUN (không được yêu cầu).

## 2026-09-24 handoff — Nút Tải về máy mở Quick Look trên iOS
- Task `cambida-ios-download-quicklook-fix-20260924`: Đã sửa lỗi nút `#mergedDownloadBtn` trên iPhone trong [1.py].
- Bằng cách gỡ bỏ thuộc tính HTML `download` trên iOS nhưng vẫn dùng `/download/<filename>`, trình duyệt Zalo/WKWebView sẽ điều hướng và mở trình xem tệp Quick Look MP4.
- Tại màn hình Quick Look này, người dùng bấm Chia sẻ (hoặc "Thêm...") -> chọn "Lưu video" để lưu vào Thư viện Ảnh (Photos).
- Máy chủ đang chạy cổng 8004 sau khi restart.

## 2026-09-24 handoff — QR nội bộ tự chuyển hướng sang HTTPS
- Khách dùng mã QR cũ dán trên bàn vẫn hoạt động: máy chủ nhận request từ mạng nội bộ và trả về 302 redirect sang HTTPS.
- Khi đổi tên miền cố định trong tương lai, chỉ cần cập nhật trường `public_base_url` trong `config.json`.

## 2026-09-24 handoff — Đóng gói phát hành 2.1.5 & Quy tắc phiên bản theo commit
- Đóng gói toàn diện phiên bản **2.1.5** vào thư mục: `D:\1\cambida\release\2.1.5` (onedir format) và `staging\2.1.5`.
- Tệp nén đính kèm: `release\2.1.5.zip` (và lưu bản sao `release\2.1.5\2.1.5.zip`).
- File thực thi: `CCTV_2.1.5.exe` (SHA-256: `B4C575B47C987B2D1218E6231383884D2F4B4EF6547C5249D4221E37FD37C19F`).
- SHA-256 file zip: `55E25E7D64FB1985AA38BE2F1BEB0A90555C01FB71006470B282133A04A271ED`.
- Tính năng bao gồm:
  + Module hóa camera (`camera_modules/`).
  + Công tắc riêng tư bàn (Privacy Switch) trên trang chủ `home.html` và backend `1.py` (Bật = Đỏ, Tắt = Xám).
  + Giao diện thêm camera đa hãng trong trang quản trị `admin.html` (quét LAN, kiểm tra kênh probe, chọn nhiều kênh).
  + Giới hạn số bàn tối đa theo mã Telegram pin `/N`.
  + Tự động chuyển hướng QR sang HTTPS và fallback HTTP LAN.
- An toàn cấu hình (Zero Config Pollution): Không chứa `config.json`, `analytics.db`, `logs`, `cctv_videos`, `nvr_cache`.
- **Quy tắc phiên bản theo commit**: Kể từ bản 2.1.5 này, mỗi commit tiếp theo sẽ tự động tịnh tiến phiên bản mới (2.1.6, 2.1.7...).

## 2026-09-24 handoff — Sửa lỗi cú pháp xem lại, tích hợp Cloudflared & Đóng gói 2.1.51
- Đã khắc phục lỗi cú pháp thừa dấu `}` trong hàm xử lý `iosSaveShareBtn` ở `index.html` và template nhúng trong `1.py`.
- Khôi phục hoạt động cho trình duyệt Cốc Cốc: ngày tự động gán hôm nay, thước đo timeline hiển thị đầy đủ vạch chia và vệt video xanh, kim giờ hoạt động chính xác.
- Kiểm thử hồi quy JavaScript: 26/26 tests PASS (`tests/replay_timeline_ui.test.js`).
- Tích hợp trọn gói `cloudflared.exe` và thư mục `cloudflared_setup/` vào bản phát hành.
- Đóng gói hoàn tất vào `D:\1\cambida\release\2.1.51` và file nén `release\2.1.51.zip` (SHA-256: `3F877312E3F11848AB0592AF9427F07555927E24941205E9DA3C8CD58E76B7FD`).
- SHA-256 EXE: `09BC1712A71C29E231691181EF7C06CB14C352E4DEE07F64344080143E70A562`.
- Server 8004 đã được khởi động lại với mã mới (PID 16052).
