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
- Công việc Codex phải đi qua Codex MCP trực tiếp sau khi liveness gate PASS; CLI chỉ được dùng khi người dùng cho phép rõ ràng cho chính task đó và không miễn MCP gate.
- Công việc AGY phải đi qua AGY MCP trực tiếp sau khi liveness gate PASS; CLI chỉ được dùng khi người dùng cho phép rõ ràng cho chính task đó và không miễn MCP gate.
- Yato/process manager chỉ giữ vai trò local system/process và giám sát CLI khi CLI đã được người dùng cho phép rõ ràng cho task cụ thể; không dùng để lách MCP gate.
- Remote chỉ dùng khi cần GUI/visual; không thay thế executor/MCP đã chọn nếu chưa có chỉ thị của người dùng.
- Direct AGY/Codex MCP là route thực thi mặc định và bắt buộc cho task tương ứng; không tự fallback sang CLI hay executor khác khi MCP lỗi.
- Mỗi task AGY/Codex giữ `task_id` và `process_id` khi transport cung cấp; theo dõi đúng session/status/log tới kết quả terminal thực sự.
- Không để nhiều executor cùng sửa một vùng source khi chưa có kế hoạch integration.
- Mỗi task chỉ được `COMPLETED` sau khi đã tạo Git commit riêng cho thay đổi thuộc scope task; không commit lẫn thay đổi ngoài scope.
- Không coi việc im lặng quá 10 phút là `TIMEOUT` và không tự kill/cancel executor vì im lặng. Với task AGY/Codex kéo dài, checkpoint tiến độ ở phút 15, 30, 45... trên chính session đang chạy; chỉ kết luận `TIMEOUT` khi đã kiểm tra status/log và có bằng chứng task thực sự mất khả năng tiếp tục theo orchestration hiện hành.
- Build/lint/unit test không được gọi là E2E/device test.
- Protocol Drive queue/manifest/sync barrier/ACK cũ đã RETIRED.

## Bộ nhớ dự án

Đọc theo thứ tự: `PROJECT → DECISIONS → STATE → TASKS → HANDOFF`.

## 2026-09-19 — Quy tắc điều phối thay thế

- Nguồn quy tắc toàn cục duy nhất: `D:\1\gptagycodex.md`; AGENTS.md chỉ chứa hướng dẫn dự án và dẫn chiếu, không sao chép toàn bộ global spec.
- Mọi parent task mới đọc FAST BOOT trước; trước xác nhận chỉ diễn giải và phân công dự kiến, không kiểm tra target/task tool. Dấu phẩy đầu/cuối lệnh kích hoạt COMMA BYPASS.
- Sau xác nhận kiểm tra sống công cụ bắt buộc; lỗi thì dừng, không tự fallback. Sau đó mới đọc `.project` và Source/Git, tiếp tục task đang chạy nếu có.
- AGY/Codex dùng MCP tương ứng làm route bắt buộc; CLI chỉ khi được người dùng cho phép rõ ràng cho task cụ thể sau khi MCP gate đã PASS. Checkpoint 15 phút, Telegram gộp theo parent; một final event khi terminal.
- Mọi quy tắc cũ trong tài liệu dự án trái FAST BOOT mới bị thay thế, trừ ràng buộc riêng Cambida không xung đột.

## Quy ước đóng gói hiện hành

- Release production giữ dòng **2.1.0** tới khi có bằng chứng NetSDK live end-to-end gồm credential hợp lệ, snapshot/live frame và MP4/ffprobe; không bump 3.x chỉ từ unit test.
- PyInstaller dùng `CCTV_2.1.0.spec` dạng onedir; UI HTML và `ffmpeg.exe` phát hành cạnh EXE, vendor NetSDK bundle trong `_internal/vendor/dahua_netsdk/`.
- Khi rebuild phải giữ nguyên `config.json`, thư mục media và dữ liệu runtime hiện có; chỉ thay executable/runtime files cần thiết.

## 2026-09-21 — Đồng bộ strict MCP gate
- Global orchestration source: `D:\1\gptagycodex.md`, `SPEC_VERSION=2026-09-18.3`, patch `2026-09-21-STRICT-MCP-AGY-CODEX-TELEGRAM-GATE`.
- AGY work bắt buộc AGY MCP ONLINE; Codex work bắt buộc Codex MCP ONLINE. Không tự retry bằng transport khác, không tự fallback CLI, không tự đổi executor.
- CLI chỉ hợp lệ khi người dùng cho phép rõ ràng cho đúng task và vẫn phải PASS MCP online gate trước khi chạy.
- Nếu MCP/tool bắt buộc lỗi hoặc offline: dừng trước thực thi và phát cảnh báo Telegram theo global spec; nếu Telegram lỗi phải báo rõ chưa gửi được.
- Đây chỉ là dấu mốc đồng bộ local; chi tiết quy tắc không sao chép vào dự án và luôn đọc FAST BOOT từ global file cho parent task mới.
