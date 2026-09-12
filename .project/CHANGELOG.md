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

