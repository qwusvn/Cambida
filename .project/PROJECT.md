# PROJECT — Cambida

Workspace duy nhất: `D:\1\cambida`

## Mục tiêu

Phát triển và duy trì Cambida/CCTV hiện có, bảo toàn hành vi đã chốt, ưu tiên ổn định, vận hành thực tế và kiểm chứng đúng loại.

## Thứ tự ưu tiên

1. Yêu cầu mới nhất của người dùng.
2. Chỉ thị riêng của project.
3. Source + Git thực tế.
4. `.project`.
5. CodeGraph index.
6. Quy chuẩn điều phối chung.
7. Thông tin cũ trong hội thoại/memory.

## Quy tắc cố định

- Source + Git trong `D:\1\cambida` là source of truth.
- `.project` là bộ nhớ vận hành chính.
- CodeGraph chỉ là code intelligence; nếu có `.codegraph` thì ưu tiên dùng trước task coding/phân tích lớn, không tự `codegraph init`.
- ChatGPT là orchestrator/architect/reviewer và quyết định cuối.
- Codex MCP là executor chính cho coding/core; mặc định `gpt-5.6-luna`, reasoning `max`.
- AGY MCP dùng cho khảo sát/UI/prototype/test/module độc lập; mặc định Gemini 3.8 Medium.
- Yato dùng cho system/ADB/process/service/port/environment/script/recovery.
- Remote chỉ dùng khi cần GUI/visual; ưu tiên Codex Computer Use nếu phù hợp.
- Không mặc định đi qua Yato để gọi Codex/AGY khi MCP trực tiếp đã có capability.
- Task Codex/AGY bắt buộc có `task_id`.
- Không để nhiều executor cùng sửa một vùng source khi chưa có kế hoạch integration.
- Mỗi task chỉ được `COMPLETED` sau khi đã tạo Git commit riêng cho thay đổi thuộc scope task; không commit lẫn thay đổi ngoài scope.
- `TIMEOUT` = quá 10 phút không có phản hồi/cập nhật; recovery phải đúng task/process tree.
- Build/lint/unit test không được gọi là E2E/device test.
- Protocol Drive queue/manifest/sync barrier/ACK cũ đã RETIRED.

## Bộ nhớ dự án

Đọc theo thứ tự: `PROJECT → DECISIONS → STATE → TASKS → HANDOFF`.
