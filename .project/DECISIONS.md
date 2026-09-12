# DECISIONS — Cambida

## 2026-09-12 — Chuẩn điều phối hiện hành

- Thứ tự ưu tiên khi xung đột: yêu cầu mới nhất của người dùng → chỉ thị riêng project → source/Git thực tế → `.project` → CodeGraph index → quy chuẩn chung → thông tin cũ.
- Bộ `.project` là bộ nhớ chính; source + Git trong `D:\1\cambida` là source of truth.
- Nếu `.codegraph` tồn tại, ưu tiên `codegraph_explore` trước task coding/phân tích lớn; CodeGraph không phải executor và không ghi đè source/Git. Không tự `codegraph init`.
- ChatGPT là orchestrator/architect/reviewer và quyết định cuối.
- Codex MCP là executor coding/core chính; mặc định `gpt-5.6-luna`, reasoning `max`; Browser/Computer Use ưu tiên Codex khi phù hợp.
- AGY MCP là executor cho khảo sát, prototype, UI, API integration, docs, unit test, điều tra lỗi và module độc lập; mặc định Gemini 3.8 Medium, chỉ nâng High khi cần.
- Yato tập trung process/service/port/environment/system dependency/ADB/emulator/script/recovery; không là tầng trung gian mặc định để gọi Codex/AGY.
- Remote chỉ dùng khi cần GUI/visual.
- Mỗi task Codex/AGY phải có `task_id`; trạng thái chuẩn: QUEUED, RUNNING, COMPLETED, FAILED, BLOCKED, CANCELLED, TIMEOUT.
- `TIMEOUT` = quá 10 phút không có phản hồi/cập nhật; phải kiểm tra status/output trước khi cancel/recovery và chỉ dừng đúng process tree của task.
- Mỗi task chỉ được chuyển sang `COMPLETED` sau khi đã tạo Git commit riêng cho đúng scope của task; không stage/commit thay đổi ngoài scope. Nếu commit thất bại thì task chưa được coi là hoàn thành.
- Telegram chỉ gửi COMPLETED / FAILED / TIMEOUT; phải có `Task:` tiếng Việt và nút `Mở hội thoại` nếu lấy được URL thật.
- Drive queue / manifest / sync barrier / ACK cũ đã RETIRED.

## 2026-09-12 — Luồng cắt video chính xác

- Mốc cắt từ UI phải giữ chính xác tới giây; không làm tròn xuống phút.
- Backend `/merge` chỉ cắt phần giao với khoảng người dùng chọn, không ghép nguyên segment ngoài vùng chọn.
- Với nhiều segment, chỉ ghép media thực có; nếu có gap thì gửi SSE `notice`.
- Ưu tiên độ chính xác mốc cắt hơn stream-copy thuần túy: từng part re-encode H.264/AAC rồi concat bằng copy khi cần.

## 2026-09-12 — Quy tắc giữ tiến trình AGY/Codex

- Task điều phối qua MCP AGY hoặc MCP Codex phải được giữ chạy cho đến khi tự chuyển sang trạng thái COMPLETED hoặc FAILED.
- Không tự ý kill, cancel hoặc 	erminate chỉ vì chưa có phản hồi hoặc cập nhật trong một khoảng thời gian.
- Khi nghi task bị treo, bắt buộc kiểm tra status trước; chỉ theo dõi/đọc output để xác minh, không tự kết thúc tiến trình.
- Quy tắc này áp dụng riêng cho task điều phối qua MCP AGY và MCP Codex, có ưu tiên hơn quy tắc timeout/cancel cũ nếu xung đột.
