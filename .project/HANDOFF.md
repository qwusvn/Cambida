# HANDOFF — Cambida

Cập nhật checkpoint: 2026-09-12 17:25 +07

## Bắt đầu hội thoại/agent mới
1. Xác nhận workspace tuyệt đối `D:\1\cambida`.
2. Đọc theo thứ tự: `.project/PROJECT.md` → `DECISIONS.md` → `STATE.md` → `TASKS.md` → `HANDOFF.md`.
3. Đối chiếu source + Git thực tế trước khi hành động; source/Git là source of truth.
4. Có `.codegraph` thì ưu tiên `codegraph_explore` trước task coding/phân tích lớn.
5. Không ghi đè thay đổi ngoài scope; mỗi task chỉ được `COMPLETED` sau khi có Git commit riêng, chỉ chứa file/thay đổi thuộc scope task.
6. Với MCP AGY/Codex: giữ task chạy tới `COMPLETED` hoặc `FAILED`; nếu nghi treo phải kiểm tra status/output trước, không tự kill/cancel chỉ vì im lặng.

## Trạng thái bàn giao
- Không có task executor đang `RUNNING` trong workspace ở thời điểm checkpoint.
- UX/UI Xem lại/Cắt video theo mockup mới đã hoàn tất và qua vòng kiểm chứng cuối.
- `index.html` hiện có: source/server selector trên header; badge động `Trực tiếp/Xem lại`; live in-place; timeline với kim cố định giữa viewport và track kéo bên dưới; bỏ thumbnail; khoảng có video tô xanh theo trục 24 giờ, khoảng trống không tô màu; zoom timeline bằng icon; tua 2×/4× tiến/lùi; pinch zoom/pan video; giữ date/download/cut/exact-cut.
- Kiểm chứng mới nhất: Chromium 430×900 xác nhận marker giữ x=215 px trong khi track dịch 80 px khi kéo, time bubble đổi theo vị trí, 0 ảnh trong filmstrip và block video xanh đúng; unittest **29/29** đạt; JavaScript syntax đạt; `git diff --check -- index.html` đạt.
- Screenshot kiểm chứng: `ui_cdp_replay.png`, `ui_cdp_cut.png`.
- Git branch `main`; working tree chưa clean. Tracked modified: `1.py`, `AGENTS.md`, `PROJECT_SYNC_PROTOCOL.md`, `index.html`; nhiều helper/profile/screenshot vẫn untracked.
- Chưa commit.

## Việc tiếp theo hợp lệ
- Nếu người dùng yêu cầu chỉnh tiếp UX/UI: dùng ảnh mockup gốc làm visual source of truth, kiểm tra source/Git trước rồi mới sửa.
- Nếu yêu cầu phát hành: build/package theo quy trình release riêng và kiểm thử trên dữ liệu/camera production.
- Nếu dọn workspace: chỉ xóa artifact/helper đã xác minh là tạm, không xóa hàng loạt.
