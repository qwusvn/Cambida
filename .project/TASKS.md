# TASKS — Cambida

## DONE

- [x] 2026-09-12 — Đồng bộ lại quy chuẩn điều phối mới nhất vào `AGENTS.md` và `.project`: bổ sung thứ tự ưu tiên khi xung đột, vai trò CodeGraph, ưu tiên công cụ, Codex/AGY MCP trực tiếp, Yato system/recovery, `task_id`, timeout/recovery và checkpoint.
- [x] 2026-09-12 — Xác nhận workspace `D:\1\cambida`.
- [x] 2026-09-12 — Loại bỏ chuẩn điều phối cũ khỏi `AGENTS.md` và `PROJECT_SYNC_PROTOCOL.md`.
- [x] 2026-09-12 — Chuyển `SYNC_STATE.json` thành compatibility tombstone.
- [x] 2026-09-12 — Khởi tạo bộ nhớ dự án `.project` theo chuẩn mới.
- [x] 2026-09-12 — Rework cấu trúc chính `index.html` theo mockup màn Xem lại/Cắt video.
- [x] 2026-09-12 — Sửa luồng cắt chính xác tới giây ở frontend và backend `/merge`.
- [x] 2026-09-12 — Kiểm chứng cắt 1 segment, nhiều segment có khoảng trống và tải file kết quả.
- [x] 2026-09-12 — Sửa regression hiển thị nguồn NVR sau redesign.
- [x] 2026-09-12 — Hoàn thiện lỗi chọn ngày/kéo-thả timeline từ vòng Codex dang dở và retest trình duyệt.
- [x] 2026-09-12 — Hoàn thiện UX/UI theo mockup mới: header/source selector, badge mode, live in-place, timeline scrub/zoom, tua 2×/4×, pinch zoom/pan video.
- [x] 2026-09-12 — Kiểm tra Jinja/render và JavaScript syntax: đạt.
- [x] 2026-09-12 — Chạy toàn bộ `unittest`: **29/29 đạt**.
- [x] 2026-09-12 — `python -m py_compile 1.py` và `git diff --check -- index.html`: đạt.
- [x] 2026-09-12 — CDP browser verify viewport 430×900: live/source/timeline/cut interactions đạt, không console error/exception.
- [x] 2026-09-12 — Chụp và đăng ký screenshot kiểm chứng `ui_cdp_replay.png` và `ui_cdp_cut.png`.
- [x] 2026-09-12 — Sửa riêng timeline theo yêu cầu mới: kim giữa cố định, kéo track bên dưới kim, bỏ thumbnail, tô xanh đúng các khoảng có video và để trống các khoảng không có video; Chromium interaction verify + 29/29 unittest đạt.
- [x] 2026-09-12 — Cập nhật luật Git: mọi task chỉ được `COMPLETED` sau khi tạo commit riêng đúng scope; commit hash bắt buộc trong báo cáo hoàn tất.

## DOING

- Không có.

## TODO

- [ ] Khi cần phát hành bản chạy mới: build/package theo quy trình release riêng và kiểm thử trên dữ liệu/camera production trước khi triển khai.
- [ ] Chỉ dọn các profile/screenshot/helper kiểm chứng cũ còn lại khi xác định rõ không phải artifact cần giữ; không xóa hàng loạt ngoài scope.
- [ ] Nếu cần độ khớp thị giác tuyệt đối với mockup, thực hiện thêm pixel-diff/visual comparison trực tiếp với ảnh nguồn.

## BLOCKED

- Không có blocker hiện tại.
