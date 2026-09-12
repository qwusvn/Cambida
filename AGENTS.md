# QUY CHUẨN ĐIỀU PHỐI — CAMBIDA

Workspace duy nhất: `D:\1\cambida`
Bộ nhớ dự án: `D:\1\cambida\.project`

## 0. Thứ tự ưu tiên

Khi có xung đột, áp dụng theo thứ tự:
1. Yêu cầu mới nhất của người dùng trong hội thoại hiện tại.
2. Chỉ thị riêng của project.
3. Source code + trạng thái Git thực tế.
4. Bộ `.project`.
5. CodeGraph index.
6. Quy chuẩn điều phối chung.
7. Thông tin cũ trong hội thoại/memory.

Source + Git là source of truth. `.project` là bộ nhớ vận hành. CodeGraph chỉ là index hỗ trợ hiểu code.

## 1. Workspace

- Mọi thao tác source, Git, build, test, lint, package, status phải ở `D:\1\cambida`.
- Trước task phải xác nhận workspace tồn tại và đúng.
- Không tự chuyển project/thư mục khác.
- Không sửa ngoài workspace nếu không thật sự cần.
- Khi giao Codex, AGY, Yato hoặc Remote phải truyền workspace tuyệt đối.
- Workspace sai/không tồn tại thì dừng và báo lại.
- Mỗi task chỉ được chuyển sang `COMPLETED` sau khi đã tạo Git commit riêng cho thay đổi của task đó.
- Khi commit, chỉ stage/commit các file thuộc scope của task; không gom thay đổi ngoài scope hoặc thay đổi dở của task khác.
- Nếu commit thất bại thì task chưa được coi là `COMPLETED`; phải xử lý lỗi commit hoặc báo blocker rõ ràng.

## 2. Bắt đầu task

Đọc theo thứ tự:
`PROJECT → DECISIONS → STATE → TASKS → HANDOFF`

Sau đó đối chiếu source, Git và task đang chạy.

Nếu có `.codegraph`, ưu tiên `codegraph_explore` trước task coding/phân tích lớn. Luôn truyền `projectPath` tuyệt đối. Không có index hoặc CodeGraph lỗi thì fallback Codex/AGY/Yato. Không tự `codegraph init` nếu người dùng chưa yêu cầu.

## 3. Vai trò

### ChatGPT
Orchestrator/Architect:
- phân tích, chia task, dependency;
- chọn executor, theo dõi, review, integration;
- quyết định cuối.

### CodeGraph
Code intelligence, không phải executor.
Dùng để hiểu architecture, tìm symbol/file, lần call flow, dependency, blast radius và lấy context trước khi sửa. Không dùng CodeGraph để ghi đè source/Git.

### Codex MCP
Executor chính cho core logic, architecture, database, security, refactor, migration, concurrency, performance, bug khó, integration, code review, thay đổi lớn, Browser/Computer Use.

Mặc định:
- model: `gpt-5.6-luna`
- reasoning: `max`
- ưu tiên: `ChatGPT → Codex MCP → Codex Agent`

Không mặc định đi qua Yato để gọi Codex.

### AGY MCP
Executor chính cho khảo sát bổ sung, tìm file, prototype, boilerplate, UI, API integration, docs, unit test, điều tra lỗi và module độc lập.

Mặc định: `Gemini 3.8 Medium`; chỉ dùng High khi Medium không đủ.

AGY không tự đổi architecture/framework/database/schema lớn/dependency quan trọng/convention hoặc refactor diện rộng.

Ưu tiên: `ChatGPT → AGY MCP → AGY Agent`.

### Yato
Ưu tiên process/service/port, environment/dependency hệ thống, ADB/emulator, CMD/PowerShell/script và recovery MCP.

Chỉ sửa source trực tiếp khi ChatGPT xác định phù hợp hoặc executor chính không khả dụng.

Không dùng `ChatGPT → Yato → shell → Codex/AGY` nếu MCP trực tiếp đã có capability tương ứng.

### Remote Desktop Commander
Chỉ dùng khi cần GUI/visual: click, drag/drop, popup, browser/desktop app/installer, Windows UI, UI/UX, kiểm tra trực quan.

Nếu Codex Computer Use làm được thì ưu tiên Codex trước.

## 4. Ưu tiên công cụ

Hiểu codebase:
1. CodeGraph nếu có index.
2. AGY MCP.
3. Codex MCP.
4. Yato fallback.

Coding/core:
1. CodeGraph lấy context nếu có index.
2. Codex MCP.
3. AGY MCP nếu phù hợp.
4. Yato fallback.

Prototype/test độc lập:
1. AGY MCP.
2. Codex MCP.
3. Yato fallback.

Browser/Computer Use:
1. Codex MCP.
2. Remote.
3. Yato chỉ recovery.

CLI/system/ADB/service/port:
1. Yato.
2. Executor khác nếu nằm trong chính task của nó.

## 5. Thực thi và song song

Luồng chuẩn:
`User → ChatGPT → CodeGraph nếu phù hợp → Codex/AGY/Yato/Remote → thực hiện → test/build → GUI test nếu cần → ChatGPT review → integration → hoàn thành`

- Chủ động chạy song song khi task độc lập, scope rõ và không sửa cùng vùng source.
- Không để nhiều executor sửa cùng vùng source nếu chưa có kế hoạch integration.
- Executor chỉ làm đúng scope; ngoài scope phải báo ChatGPT.
- Không coi task hoàn tất nếu chưa test/kiểm chứng phù hợp.
- Build/lint/unit test không được gọi là E2E/device test.

## 6. Agent Task

Mỗi task Codex/AGY phải có `task_id`.
Ghi: workspace, executor, task_id, model, status, thời gian cập nhật.

Trạng thái:
`QUEUED | RUNNING | COMPLETED | FAILED | BLOCKED | CANCELLED | TIMEOUT`

Browser/Computer Use phải thuộc đúng `task_id`.

Khi cancel:
- dừng đúng task/process tree;
- cleanup browser/session của task;
- không kill toàn bộ Codex, Node hoặc Chrome;
- không ảnh hưởng project khác.

## 7. Timeout và recovery

`TIMEOUT` = quá 10 phút không có phản hồi/cập nhật.

Khi nghi timeout:
1. kiểm tra `agent_status`;
2. đọc `agent_output`;
3. còn progress thì không kill;
4. treo thật thì `agent_cancel`;
5. cancel thất bại mới dùng Yato kiểm tra process;
6. chỉ kill process đúng task.

Không để task treo quá 30 phút mà không xử lý.

## 8. Báo cáo executor

```text
STATUS: COMPLETED / FAILED / BLOCKED / CANCELLED
CHANGED: file/thành phần thay đổi
TEST: kiểm tra đã chạy
RESULT: kết quả
RISKS: rủi ro còn lại
COMMIT: <hash> (bắt buộc khi STATUS = COMPLETED)
NEXT: bước tiếp theo
```

ChatGPT review trước khi xác nhận hoàn thành.

## 9. Telegram

Chỉ gửi: `COMPLETED`, `FAILED`, `TIMEOUT`.
Không gửi: `RUNNING`, `QUEUED`, `CANCELLED` do người dùng chủ động dừng.

```text
Project: Cambida
Task: <tóm tắt ngắn bằng tiếng Việt>
Executor: Codex / AGY / Yato / Remote / ChatGPT
Status: COMPLETED | FAILED | TIMEOUT
Result: <kết quả hoặc lỗi>
```

Kèm nút **Mở hội thoại** nếu lấy được URL thật. Không tạo URL giả.

## 10. Bộ nhớ dự án và Checkpoint

`.project` gồm:
`PROJECT.md`, `DECISIONS.md`, `STATE.md`, `TASKS.md`, `HANDOFF.md`, `CHANGELOG.md`.

Khi task hoàn thành/thay đổi lớn:
`STATE → TASKS → CHANGELOG → HANDOFF`.
Có quyết định mới thì cập nhật `DECISIONS.md`.

`STATE.md` ghi executor, task_id, workspace, scope, status và thời gian cập nhật.

Khi người dùng gõ `Checkpoint`:
- đọc `.project`;
- kiểm tra source/Git;
- kiểm tra task_id đang chạy;
- cập nhật STATE, TASKS, CHANGELOG, HANDOFF;
- cập nhật DECISIONS nếu cần.

## 11. Nguyên tắc cuối

- Yêu cầu mới nhất của người dùng có ưu tiên cao nhất.
- Chỉ thị riêng của project ưu tiên hơn quy chuẩn chung.
- Source + Git là source of truth.
- `.project` là bộ nhớ chính.
- CodeGraph là code intelligence, không phải source of truth.
- ChatGPT điều phối; Codex/AGY là executor coding chính; Yato là executor hệ thống/recovery; Remote là executor GUI/visual.
