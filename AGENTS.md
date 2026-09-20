# CAMBIDA — QUY TẮC DỰ ÁN HIỆN HÀNH

Workspace duy nhất: `D:\1\cambida`. Bộ nhớ vận hành: `.project/`.
Nguồn quy tắc điều phối toàn cục DUY NHẤT: `D:\1\gptagycodex.md` (đọc trực tiếp, không sao chép vào dự án).
Tài liệu này thay thế toàn bộ phiên bản AGENTS.md cũ; nếu có xung đột, áp dụng thứ tự ưu tiên trong FAST BOOT của tệp toàn cục.

## Trình tự bắt buộc

1. Chat/parent task mới: chỉ đọc từ đầu tệp toàn cục đến `FAST_BOOT_END`; chỉ đọc phần FULL khi FAST BOOT yêu cầu hoặc cần điều phối phức tạp.
2. Trước mọi yêu cầu sửa/triển khai/thực thi: diễn giải phạm vi, kết quả và phân công công cụ/agent; chờ người dùng xác nhận. Lệnh bắt đầu hoặc kết thúc bằng dấu phẩy được thực thi ngay theo COMMA BYPASS.
3. Sau xác nhận: kiểm tra nhanh sự sống của TẤT CẢ công cụ cần thiết. Với AGY/Codex, ưu tiên MCP; nếu kết nối lỗi, thử lại đúng một lần rồi mới chuyển sang CLI tương ứng khi vẫn thất bại. Công cụ bắt buộc khác lỗi thì dừng theo FAST BOOT.
4. Sau tool gate: kiểm tra workspace, `.project` (PROJECT, ARCHITECTURE nếu có, DECISIONS, STATE, TASKS, HANDOFF), source/Git và tác vụ đang chạy; không nhân đôi parent task.
5. Mặc định COLLABORATIVE; `SKIP GPT` theo FAST BOOT chuyển SINGLE_AGENT, không tự gọi agent khác.
6. AGY/Codex ưu tiên MCP, CLI chỉ là dự phòng sau một lần thử kết nối lại MCP thất bại; CLI dài hạn chạy dưới Yato/process manager; giữ task_id/process_id, log và giám sát tới khi parent task terminal thực, không dừng vì executor im lặng.
7. Với task AGY/Codex còn chạy: checkpoint 15 phút một lần, hỏi trên session cũ khi an toàn hoặc đọc status/log; tối đa một Telegram PROGRESS gộp/parent/checkpoint; không suy đoán phần trăm.
8. Chỉ báo COMPLETED sau xác minh đầu ra, kiểm thử/acceptance và Git commit riêng đúng phạm vi. Parent terminal gửi đúng một notification_final_event; không gửi child completion, không gửi conversation/share URL.

## Ràng buộc riêng của Cambida

- Source/Git là sự thật kỹ thuật; `.project` là bộ nhớ vận hành, CodeGraph chỉ cung cấp ngữ cảnh.
- Không tự sửa cấu hình camera, dữ liệu media, release, dịch vụ đang chạy hoặc source ngoài phạm vi đã duyệt.
- Không stage hoặc commit thay đổi ngoài phạm vi; không reset/kill tiến trình của task khác.
- Build/lint/unit test không được gọi là kiểm thử đầu cuối trên thiết bị thật.
- Protocol Drive queue/manifest/sync barrier/ACK cũ đã RETIRED; không tái kích hoạt.
