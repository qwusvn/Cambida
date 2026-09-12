# STATE — Cambida

Cập nhật: 2026-09-12 20:31 +07

## Trạng thái hiện tại

- Workspace đã xác nhận: `D:\1\cambida`.
- Git branch: `main`; working tree **chưa clean** do thay đổi ngoài scope còn tồn tại.
- File tracked ngoài scope đang modified: `1.py`, `PROJECT_SYNC_PROTOCOL.md`; không được gom vào commit của task timeline.
- Có nhiều artifact/profile kiểm chứng untracked (`.chrome-*`, `.project`, helper preview/CDP và screenshot); chưa dọn để tránh xóa nhầm ngoài scope.
- Không có Codex MCP task `RUNNING` trong workspace. AGY task `agy_mty35zms_33586c` đã `COMPLETED`.
- Codex MCP recovery `task_102761e1-7b24-43c0-9a88-f1c9fe96c5f1` trước đó `FAILED`; Codex CLI qua MCPWEB process `8a037da4-2629-459b-94b3-3944c1e448f9` đã `TIMED_OUT` sau 30 phút. Không còn tiến trình executor cần theo dõi cho task UX/UI này.

## Mốc hoàn tất gần nhất

- Đã thêm nút chuyển chế độ phát lại `Timeline ↔ Chọn giờ`. Mặc định giữ timeline mới; chế độ `Chọn giờ` khôi phục cách cũ gồm giờ bắt đầu + danh sách đoạn video trong ngày đang chọn, dùng chung nguồn/ngày/player hiện tại.
- Kiểm chứng Chromium 430×900 đạt: mặc định timeline hiện và panel cũ ẩn; chuyển sang `Chọn giờ` hiển thị đúng đoạn `13:06:24 - 13:07:24` cho mốc 13:00 và tải `/video/fake.mp4`; chuyển lại timeline giữ kim giữa chính xác. JavaScript syntax, `git diff --check` và unittest 29/29 đều đạt.
- Quy tắc Git hiện hành: task chỉ được `COMPLETED` sau khi có commit riêng đúng scope; báo cáo hoàn tất bắt buộc có commit hash.
- Timeline đã được chỉnh đúng yêu cầu mới nhất: kim thời gian cố định ở giữa, kéo dải timeline chạy bên dưới; bỏ thumbnail; khoảng có video hiển thị màu xanh, khoảng không có video để nền trống.
- Timeline hiện dùng trục 24 giờ của ngày đang chọn và các khoảng video từ `visibleVideos`; màn cắt vẫn giữ vùng chọn/tay nắm theo đúng thời gian tuyệt đối.
- Kiểm chứng Chromium 430×900: kim giữ nguyên tại x=215 px khi track dịch 80 px; time bubble thay đổi theo vị trí; 0 ảnh thumbnail trong timeline; vùng video có nền `rgb(22, 191, 103)`; JavaScript syntax đạt; `git diff --check -- index.html` đạt; unittest 29/29 đạt.
- UX/UI màn Xem lại/Cắt video theo mockup mới đã hoàn tất vòng triển khai + kiểm chứng cuối.
- `index.html`: source/server selector trên header; badge động `Trực tiếp/Xem lại`; live in-place; bỏ seek bar riêng; timeline scrub/drag; zoom timeline bằng icon; tua 2×/4× tiến/lùi; pinch zoom/pan video; giữ date/download/cut/exact-cut.
- Kiểm chứng cuối: `python -m py_compile 1.py` đạt; unittest **29/29** đạt; JavaScript syntax đạt; `git diff --check -- index.html` đạt.
- CDP browser verify viewport 430×900 đạt: không `backButton`, không range seek riêng, live không điều hướng khỏi trang, source menu NVR chuyển Server 1/Server 2 đúng, timeline drag/zoom/tua hoạt động, cut merge URL giữ thời gian tới giây, không console error/exception.
- Screenshot kiểm chứng hiện có: `ui_cdp_replay.png`, `ui_cdp_cut.png` và đã đăng ký artifact MCPWEB.
- Các thay đổi UX/UI trước task hiện tại đã được checkpoint riêng trong Git trước khi triển khai chế độ chọn giờ, tránh trộn scope commit.

## Rủi ro / việc còn lại

- Chưa có pixel-diff tự động trực tiếp với ảnh mockup gốc; hiện mới kiểm chứng bằng render/CDP và ảnh chụp 430×900.
- Cảnh báo `RequestsDependencyWarning` trong môi trường Python còn tồn tại nhưng không làm test fail.
- Khi cần phát hành, vẫn phải test với dữ liệu/camera production và quy trình release riêng.
