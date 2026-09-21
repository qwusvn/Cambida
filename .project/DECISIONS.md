# DECISIONS — Cambida

## 2026-09-12 — Chuẩn điều phối hiện hành

- Thứ tự ưu tiên khi xung đột: yêu cầu mới nhất của người dùng → chỉ thị riêng project → source/Git thực tế → `.project` → CodeGraph index → quy chuẩn chung → thông tin cũ.
- Bộ `.project` là bộ nhớ chính; source + Git trong `D:\1\cambida` là source of truth.
- Nếu `.codegraph` tồn tại, ưu tiên `codegraph_explore` trước task coding/phân tích lớn; CodeGraph không phải executor và không ghi đè source/Git. Không tự `codegraph init`.
- ChatGPT là orchestrator/architect/reviewer và quyết định cuối.
- Codex work dùng direct Codex MCP làm route bắt buộc sau khi liveness gate PASS; CLI chỉ khi người dùng cho phép rõ ràng cho task cụ thể và không miễn MCP gate.
- AGY work dùng direct AGY MCP làm route bắt buộc sau khi liveness gate PASS; CLI chỉ khi người dùng cho phép rõ ràng cho task cụ thể và không miễn MCP gate.
- Yato/process manager xử lý local process/service/port/environment/system dependency/ADB/emulator/script/recovery và chỉ giám sát CLI khi CLI đã được người dùng cho phép rõ ràng cho task cụ thể; không thay thế MCP gate.
- Remote chỉ dùng khi cần GUI/visual và không được tự thay thế executor đã chọn.
- Mỗi task AGY/Codex giữ `task_id` và `process_id` khi transport cung cấp; trạng thái chuẩn: QUEUED, RUNNING, COMPLETED, FAILED, BLOCKED, CANCELLED, TIMEOUT.
- Không suy ra `TIMEOUT` chỉ từ 10 phút im lặng. Với task AGY/Codex kéo dài, checkpoint tiến độ ở phút 15, 30, 45... bằng status/log hoặc input an toàn trên chính session hiện có; không restart/duplicate/kill task để hỏi tiến độ.
- Mỗi task chỉ được chuyển sang `COMPLETED` sau khi đã tạo Git commit riêng cho đúng scope của task; không stage/commit thay đổi ngoài scope. Nếu commit thất bại thì task chưa được coi là hoàn thành.
- Telegram được phép gửi một thông báo PROGRESS gộp cho parent task ở mỗi checkpoint 15 phút nếu task vẫn RUNNING và có dữ liệu thực; khi parent task terminal chỉ gửi đúng một `notification_final_event` cho COMPLETED / FAILED / TIMEOUT. Không dùng conversation/share URL trong notification.
- Drive queue / manifest / sync barrier / ACK cũ đã RETIRED.

## 2026-09-12 — Luồng cắt video chính xác

- Mốc cắt từ UI phải giữ chính xác tới giây; không làm tròn xuống phút.
- Backend `/merge` chỉ cắt phần giao với khoảng người dùng chọn, không ghép nguyên segment ngoài vùng chọn.
- Với nhiều segment, chỉ ghép media thực có; nếu có gap thì gửi SSE `notice`.
- Ưu tiên độ chính xác mốc cắt hơn stream-copy thuần túy: từng part re-encode H.264/AAC rồi concat bằng copy khi cần.

## 2026-09-19 — Quy tắc giữ tiến trình AGY/Codex

- Task AGY/Codex dùng MCP tương ứng làm route mặc định/bắt buộc; nếu CLI được người dùng cho phép riêng cho task thì vẫn giữ đúng session đến kết quả terminal thực sự: COMPLETED, FAILED hoặc TIMEOUT đã được xác minh.
- Không tự ý kill, cancel hoặc 	erminate chỉ vì chưa có phản hồi hoặc cập nhật trong một khoảng thời gian.
- Khi nghi task bị treo, bắt buộc kiểm tra đúng `task_id`/`process_id`, status và output/log trước; ưu tiên tiếp tục chính session đang chạy.
- Với task kéo dài, checkpoint ở phút 15, 30, 45...; nếu transport không nhận được prompt đồng thời thì chỉ đọc status/log hiện có, không tuyên bố đã gửi câu hỏi khi thực tế chưa gửi được. Quy tắc này thay thế các quy tắc timeout/cancel cũ; route direct AGY/Codex MCP hiện hành vẫn áp dụng.

## 2026-09-12 — Replay 1.3.42, single NVR và license Telegram ghim
- Replay UI lấy bản 1.3.42 thật từ EXE làm baseline; không tiếp tục UI timeline mới cho trạng thái hiện tại.
- Trang replay chỉ dùng **một nguồn NVR đã cấu hình sẵn**. Không hiển thị hay cho chọn Server 1/Server 2.
- Giữ nguyên mô hình cấu hình NVR per-camera/backend mới nhất; việc bỏ hai server chỉ là quyết định luồng sử dụng/UI, không rollback NVR adapter/config.
- Bản quyền xem lại dùng tin nhắn ghim Telegram làm nguồn xác thực trung tâm; key lấy lại từ Windows `MachineGuid`; không dùng cache/registry cục bộ để tự cấp quyền.
- Khi license không hợp lệ: recorder, live và admin vẫn chạy; replay/list/play/download/cut/merge bị khóa.
- Commit thực thi: `0a13a5af560fc9539b9fa815ae37dcd11b44b567`.

## 2026-09-12 — Phát hành 2.1.0
- Yêu cầu trực tiếp của người dùng nâng phiên bản phát hành lên **2.1.0**, ưu tiên hơn quy ước tăng patch 2.0.x trước đó.
- Artifact chính thức tiếp tục dùng PyInstaller onedir và chỉ lưu tại `D:\1\cambida\release\2.1.0\`.
- Source phát hành 2.1.0 bao gồm logic cắt/ghép chính xác tới giây đang có trong working source; commit release `5420e54` là mốc source tương ứng với binary đã build.

## 2026-09-12 — Gỡ bản quyền replay
- Quyết định mới nhất **thay thế** quyết định license Telegram ghim trước đó.
- Gỡ toàn bộ kiểm tra `MachineGuid`, tin nhắn ghim Telegram, license state/watcher và gate replay/timeline/cut/download.
- Gỡ lệnh Telegram `/license` và `/activate`; giữ các lệnh/cảnh báo hệ thống khác.
- Rebuild cùng phiên bản **2.1.0**; artifact mới có SHA-256 `82bb7a582387e7ec4830ba57bbdecef9975019dcd61da25cc07a08335adbb423`.
- Commit thực thi: `111d403`.

## 2026-09-12 — NVR time selection và NVR merge
- Thời gian Dahua `StartTime/EndTime` được coi là local recorder time; test thực tế xác nhận NVR clock khớp PC trong khoảng vài giây.
- Khi người dùng chọn giờ, replay phải ưu tiên segment chứa chính mốc đó; không được mặc định chọn segment mới nhất.
- Playback và download NVR phải truyền `at` theo local datetime chính xác.
- Với camera playback_source=`nvr`, `/merge` phải cắt từ NVR, không lấy nhầm file Local backup.
- Giữ giới hạn max merge hiện tại; Dahua split chunk theo `playback_chunk_sec`, sau đó chuẩn hóa và concat khi cần.
- Commit quyết định/thực thi: `f16d752`.

## 2026-09-12 — Chốt Timeline là giao diện replay duy nhất
- Replay dùng timeline làm giao diện chính; không giữ ô nhập giờ song song.
- Date picker được giữ để đổi ngày; giờ được chọn bằng kim giữa cố định và thao tác kéo timeline.
- `StartTime/EndTime` NVR là source of truth cho coverage và mốc playback/cut.
- Đoạn có bản ghi hiển thị xanh; gap không màu; không thumbnail.
- Cut dùng datetime tuyệt đối trên timeline thay vì offset trong một file, để cho phép span nhiều segment/gap.
- Khi màn hình ẩn/hiện lại (Cut→Replay), phải rerender coverage/ruler sau layout bằng `requestAnimationFrame`.
- Không sửa hoặc đồng bộ lại `config.json` trong task UI timeline; workspace và release config được bảo vệ bằng hash trước/sau deploy.
- Commits: `5efc105`, `338a842`.


## 2026-09-13 +07 ? Imou direct RTSP strategy
- Do not persist an auto-detected RTSP profile into `config.json`; preserve user config and cache only the successful profile label in RAM.
- Treat Imou as Dahua-family for RTSP path generation, but keep ONVIF and legacy/custom fallbacks for firmware/model variance.
- Keep NVR flow isolated: local direct overrides and profile fallback apply only to Local mode.
- Generated Dahua/Imou `cam/realmonitor` URLs are kept in standard form; the historical `timeout` query is retained only for legacy/custom local paths.
- Recording fallback must occur on the actual FFmpeg recording attempt, not through a recurring preflight.
- Sanitize RTSP userinfo/credentials before any recording failure reaches logs, state, or Telegram.


## 2026-09-13 12:20 +07 ? Separate Imou private SDK from RTSP
- Keep RTSP media access on port 554 and Dahua/Imou `cam/realmonitor` paths.
- Treat TCP 37777 as a distinct Dahua/Imou private SDK/control transport, not as another RTSP port.
- Do not add a fake `rtsp://...:37777/...` fallback. If 37777 video/control support is required, implement it as a separate adapter (official NetSDK or compatible DVRIP client) after explicit integration work.
- Never persist live-test credentials in project memory, source, tests or config.

## 2026-09-13 - 3.x gate for private 37777 transport
- TCP 37777 is implemented as a separate Dahua/Imou NetSDK/DVRIP adapter; it must never be represented as an RTSP URL or silently fall back to RTSP when explicitly selected.
- NetSDK is the media backend (RealPlay/SaveRealData/RealData callback); raw DVRIP is used only for normal challenge-auth diagnostics/control compatibility, not authentication bypass.
- Version 3.x is gated on live end-to-end proof against a real device: accepted private credential + received media + valid snapshot/live frame + valid recorded MP4. Unit tests alone are not sufficient for the version bump.
- Current native responses independently agree that the configured private credential is rejected, so the implementation remains BLOCKED and version stays 2.x development state.


## 2026-09-19 - Camera transport and active-config cleanup
- Camera entries are canonicalized by source and transport. Local NetSDK owns only its 37777/channel/stream fields; Local RTSP owns only its RTSP/record/preview fields; NVR owns only NVR fields. Validation and save paths reject mixed stale fields rather than silently carrying them forward.
- Explicit local_transport=netsdk never probes or falls back to RTSP. NVR replay/list/recording remains isolated, with local archive use only when backup_local is explicitly enabled.
- The authorized cleanup removed all active camera/table entries from workspace and release/embedded config copies after a restricted backup, while retaining global web/Telegram/storage settings and existing media. Empty configuration is a supported state and must not seed demo cameras.
- No 3.x release/version bump is allowed without accepted private credentials plus real snapshot/live frame and MP4/ffprobe evidence.
## 2026-09-19 — Thay thế quy tắc điều phối cũ theo global FAST BOOT

- Quyết định: `D:\1\gptagycodex.md` là nguồn toàn cục duy nhất; `AGENTS.md` mới chỉ tham chiếu FAST BOOT và giữ quy tắc riêng Cambida.
- Quy tắc xác nhận và phân công trước thực thi áp dụng cả bước kiểm tra target; COMMA BYPASS chỉ khi lệnh bắt đầu hoặc kết thúc bằng dấu phẩy.
- Tool gate nghiêm ngặt sau xác nhận: công cụ bắt buộc không phản hồi thì dừng, không tự thay thế/fallback.
- Chạy AGY/Codex qua MCP tương ứng sau strict online gate; CLI chỉ khi người dùng cho phép rõ ràng cho task cụ thể và không được dùng làm fallback khi MCP lỗi. Giữ task_id/process_id khi có, theo dõi đến kết quả xác minh; checkpoint 15 phút; Telegram một tiến độ gộp mỗi checkpoint và một final event cho parent terminal, không dùng liên kết hội thoại.
- Quy tắc CLI-only, timeout do im lặng 10 phút, auto-cancel, Telegram chỉ terminal không có checkpoint, tự fallback và nút liên kết hội thoại trong tài liệu cũ đều hết hiệu lực. Direct AGY/Codex MCP là route hiện hành theo global FAST BOOT.

## 2026-09-19 — Finalize camera/Imou/NetSDK work

- Chốt source camera/Imou/NetSDK hiện tại vào release line **2.1.0**; không tạo 3.x vì private NetSDK credential/live media vẫn chưa được xác minh.
- `CCTV_2.1.0.spec` phải bundle đủ 10 DLL dưới `vendor/dahua_netsdk` vào `_internal/vendor/dahua_netsdk`; HTML và FFmpeg tiếp tục theo convention phát hành cạnh EXE.
- Final build phải smoke-test bằng chính EXE, giữ nguyên config/media release và ghi SHA-256 artifact vào `.project`.

## 2026-09-21 — Chốt thiết kế giao diện Mobile Athena Pool Room
- Áp dụng 100% ngôn ngữ thiết kế từ bộ mockup `D:\1\cambida_ui_mockup_final.zip`:
  - Thanh điều hướng mobile đầy đủ (back, search, breadcrumb camera · site, refresh).
  - Segmented control 2 tab (Trực tiếp / Xem lại) bo tròn hiện đại; pill quay lại `● Xem lại` trên màn hình Cắt video.
  - Video player viền bo cong 20px, bóng đổ, watermark `imou` cố định, timestamp overlay hiển thị trực quan và tự động cập nhật, floating fullscreen.
  - Trực tiếp: Card trạng thái xanh mint `Đang trực tiếp - Kết nối tốt`, timeline card tinh giản tích hợp date picker và rate/zoom, bottom callout card dẫn sang Xem lại.
  - Xem lại: Hàng điều khiển chọn ngày và `Xem trực tiếp`, timeline card hiện đại, nút CTA gradient lớn `Cắt Video`.
  - Cắt video: Hàng nút back tròn + date + xem trực tiếp, timeline cắt với drag handles và mask rõ ràng, card thông tin 3 cột trực quan, CTA gradient `Cắt và tải về` cùng nút `Hủy`.
  - Bảo toàn 100% logic backend, NVR/Local mode, timeline playback, và toàn bộ suite tests hiện hữu.
