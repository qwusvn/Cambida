# Quy tắc kiểm chứng

- Build, lint hoặc unit test không tự động là bằng chứng E2E hay thành công trên thiết bị thật.

# Quy tắc Điều phối & Thực thi Hệ thống (Remote-First)

## 1. Phân vai Kiến trúc
- **Brain / Orchestrator / Reviewer**: `ChatGPT` — giữ vai trò phân tích, lập kế hoạch, chia task và review diff/log.
- **Control Plane**: `REMOTE_DESKTOP_COMMANDER` — kênh điều khiển trực tiếp tới máy Yato (`D:\1\cambida`).
- **Primary Executor**: `AGY CLI` — bắt buộc sử dụng model `gemini-3.8-flash-high`, `--mode accept-edits` khi sửa code hoặc `--mode plan` khi khảo sát.
- **Secondary Executor**: `Codex CLI` — đóng vai trò fallback hoặc đối chiếu khi cần.
- **Source of Truth**: `LOCAL_YATO_WORKTREE` trên máy Yato. Mọi revision chuẩn được xác định qua Git local worktree.
- **Google Drive**: Chỉ sử dụng cho mục đích truyền/nhận file lớn (`LARGE_FILES_ONLY`), binary hoặc artifact phục vụ cloud review. Tuyệt đối không dùng làm source-of-truth, không làm queue, không làm sync barrier hay ACK.

## 2. Quy trình Thực thi & Đảm bảo Chất lượng
- Executor trực tiếp thao tác mã nguồn, build và chạy tests trên máy Yato.
- ChatGPT tự đọc diff, test output, terminal log qua `REMOTE_DESKTOP_COMMANDER` để review mà không cần người dùng can thiệp copy/paste trung gian.
- Nếu test chưa PASS, ChatGPT tiếp tục giao vòng sửa tiếp theo cho executor.
- Tuyệt đối không khởi động lại watchdog cũ, không chạy sync barrier 3 nút hay manifest/ACK trên Drive.
- Khi máy Yato offline, dữ liệu trên Drive không được coi là source code mới nhất.
