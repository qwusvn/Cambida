# CAMBIDA — QUY TẮC DỰ ÁN HIỆN HÀNH

Workspace duy nhất: `D:\1\cambida`. Bộ nhớ vận hành: `.project/`.
Nguồn quy tắc điều phối toàn cục DUY NHẤT: `D:\1\gptagycodex.md` (đọc trực tiếp, không sao chép vào dự án).
Tài liệu này thay thế toàn bộ phiên bản AGENTS.md cũ; nếu có xung đột, áp dụng thứ tự ưu tiên trong FAST BOOT của tệp toàn cục.

## Trình tự bắt buộc

1. Chat/parent task mới: chỉ đọc từ đầu tệp toàn cục đến `FAST_BOOT_END`; chỉ đọc phần FULL khi FAST BOOT yêu cầu hoặc cần điều phối phức tạp.
2. Trước mọi yêu cầu sửa/triển khai/thực thi: diễn giải phạm vi, kết quả và phân công công cụ/agent; chờ người dùng xác nhận. Lệnh bắt đầu hoặc kết thúc bằng dấu phẩy được thực thi ngay theo COMMA BYPASS.
3. Sau xác nhận: kiểm tra nhanh sự sống của TẤT CẢ công cụ cần thiết. AGY bắt buộc AGY MCP ONLINE/PASS; Codex bắt buộc Codex MCP ONLINE/PASS. Nếu MCP/công cụ bắt buộc lỗi, offline hoặc không phản hồi hợp lệ thì DỪNG trước thực thi và đi theo Telegram tool-gate failure của FAST BOOT; không tự đổi transport, executor hoặc fallback CLI.
4. Sau tool gate: kiểm tra workspace, `.project` (PROJECT, ARCHITECTURE nếu có, DECISIONS, STATE, TASKS, HANDOFF), source/Git và tác vụ đang chạy; không nhân đôi parent task.
5. Mặc định COLLABORATIVE; `SKIP GPT` theo FAST BOOT chuyển SINGLE_AGENT, không tự gọi agent khác.
6. AGY/Codex dùng MCP tương ứng làm route thực thi mặc định và bắt buộc. CLI chỉ được dùng khi người dùng cho phép rõ ràng cho đúng task cụ thể, và quyền CLI không miễn MCP online gate. Giữ task_id/process_id khi transport cung cấp, log và giám sát tới khi parent task terminal thực; không dừng chỉ vì executor im lặng.
7. Với task AGY/Codex còn chạy: checkpoint 15 phút một lần, hỏi trên session cũ khi an toàn hoặc đọc status/log; tối đa một Telegram PROGRESS gộp/parent/checkpoint; không suy đoán phần trăm.
8. Chỉ báo COMPLETED sau xác minh đầu ra, kiểm thử/acceptance và Git commit riêng đúng phạm vi. Parent terminal gửi đúng một notification_final_event; không gửi child completion, không gửi conversation/share URL.

## Ràng buộc riêng của Cambida

- Source/Git là sự thật kỹ thuật; `.project` là bộ nhớ vận hành, CodeGraph chỉ cung cấp ngữ cảnh.
- Không tự sửa cấu hình camera, dữ liệu media, release, dịch vụ đang chạy hoặc source ngoài phạm vi đã duyệt.
- Không stage hoặc commit thay đổi ngoài phạm vi; không reset/kill tiến trình của task khác.
- Build/lint/unit test không được gọi là kiểm thử đầu cuối trên thiết bị thật.
- Protocol Drive queue/manifest/sync barrier/ACK cũ đã RETIRED; không tái kích hoạt.

## 2026-09-22 — Cập nhật điều phối
- Nguồn hiệu lực cho phiên ChatGPT: `D:\1\gptagycodex.md` phiên bản `2026-09-22.1`; `D:\1\Cambida\gptagycodex.md` chỉ là bản sao tham khảo do người dùng yêu cầu.
- SOL và YATO là một chủ thể; YATO là giao diện công cụ. AGY/Codex chỉ là tác nhân được giao việc khi cần. Phiên AGY/Codex độc lập không thuộc quy tắc điều phối ChatGPT.
- MCP và CLI đều có thể được SOL chọn trong phạm vi đã được xác nhận; không còn bắt buộc MCP của AGY/Codex phải trực tuyến nếu không sử dụng chúng. Lựa chọn tuyến Remote Desktop Commander cụ thể của người dùng được áp dụng cho tác vụ hiện tại.
- Chỉ chạy kiểm thử, regression, build để kiểm thử hoặc thử trên thiết bị khi người dùng yêu cầu rõ ràng và xác nhận. Việc đọc, kiểm tra nội dung tệp và đối chiếu Git không phải kiểm thử.
- Các phát biểu cũ tại tệp này về `SKIP GPT`, mặc định COLLABORATIVE, bắt buộc AGY/Codex MCP, cấm chọn CLI, và bắt buộc kiểm thử để hoàn tất không còn hiệu lực. Thực hiện theo FAST BOOT phiên bản hiện hành khi có xung đột.
