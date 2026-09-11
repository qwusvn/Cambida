# Camera Bida — Quy tắc agent

Workspace duy nhất: `D:\1\Cambida`

## 1. Điều phối

- `ChatGPT` là orchestrator/architect/reviewer và là nơi quyết định cuối cùng.
- Source of truth duy nhất là source code + Git thực tế trên Yato trong `D:\1\Cambida`.
- Không giả định source vẫn giống lần đọc trước; kiểm tra trạng thái thực tế trước các task kỹ thuật khi cần.
- Watchdog/Drive queue/ACK/manifest/sync barrier cũ đã RETIRED; không tự khởi động lại.

## 2. Chọn executor

### Yato
Ưu tiên cho mọi việc CLI: file, search, Git, branch/worktree, build, test, lint, package, PowerShell/CMD, process/service/port/log, dependency, ADB và automation.

### Codex — Senior Engineer
Model mặc định: `Luna Max`.

Ưu tiên cho core logic, architecture, database, authentication, security, refactor, migration, concurrency, performance, bug khó, integration, code review và thay đổi phạm vi ảnh hưởng lớn. Có thể dùng Computer Use cho chu trình code → build → GUI → fix.

### AGY — Fast Engineer
Model mặc định: `Gemini 3.8 Medium`.

Ưu tiên khảo sát codebase, tìm file, prototype, boilerplate, UI, API integration, documentation, unit test, điều tra lỗi, module độc lập và thử nghiệm nhanh.

AGY không được tự ý đổi architecture/framework/database/schema lớn/dependency quan trọng/convention toàn dự án hoặc mở rộng scope. Chỉ nâng lên `Gemini 3.8 High` khi Medium không đủ hoặc bài toán thực sự khó/lớn.

### Remote Desktop Commander
Chỉ dùng khi thực sự cần GUI: click, drag/drop, popup, browser, desktop app, installer, Windows UI, visual/UI verification hoặc phần mềm không có CLI phù hợp.

Không dùng Remote cho Git/build/chỉnh source/quản lý file/chạy command/process/service nếu Yato làm được.

## 3. Concurrency và scope

- Task độc lập nên chạy song song khi có lợi.
- Task có dependency/chung subsystem phải chạy tuần tự phù hợp.
- Nếu nhiều agent cùng làm, ưu tiên branch/worktree riêng và phạm vi file rõ ràng.
- Không để hai executor đồng thời sửa cùng vùng source khi chưa có kế hoạch integration.
- Executor chỉ làm đúng task được giao; phát hiện vấn đề ngoài scope thì ghi nhận và báo ChatGPT, không tự mở rộng sửa.

## 4. Luồng chuẩn

Người dùng → ChatGPT phân tích → giao executor → thực hiện → build/test → GUI test nếu cần → ChatGPT review → sửa nếu cần → integration → hoàn thành.

## 5. Quy tắc kiểm chứng

- Build, lint hoặc unit test không tự động là bằng chứng E2E hay thành công trên thiết bị thật.
- Nếu yêu cầu test trực tiếp/device thật/GUI thật thì phải thực hiện đúng loại kiểm chứng đó trước khi báo PASS.

## 6. Report executor

Khi hoàn thành nên báo rõ:
- `STATUS:` COMPLETED / FAILED / BLOCKED
- `CHANGED:` file/thành phần đã thay đổi
- `TEST:` kiểm tra đã chạy
- `RESULT:` kết quả
- `RISKS:` rủi ro còn lại nếu có
- `COMMIT:` commit hash nếu có
- `NEXT:` việc tiếp theo nếu cần

## 7. Telegram task notification

Mỗi task phải có cơ chế thông báo Telegram.

Chỉ gửi khi `COMPLETED`, `FAILED` hoặc `TIMEOUT`. Không gửi trạng thái RUNNING bình thường.

`TIMEOUT` = quá 10 phút không có phản hồi/cập nhật từ executor; có cập nhật thì mốc 10 phút tính lại.

Nội dung ngắn gọn đúng dạng:

```text
Project: Camera Bida
Executor: Codex / AGY / Yato / Remote / ChatGPT
Status: COMPLETED | FAILED | TIMEOUT
Result: <tóm tắt kết quả hoặc lỗi>
```

Mỗi thông báo phải kèm nút `Mở hội thoại`, ưu tiên đúng conversation ChatGPT đang điều phối.

## 8. Quy ước release

- Bản phát hành chính thức hiện tại: `2.0.0`.
- Bản tiếp theo mặc định tăng patch: `2.0.1`, `2.0.2`, ...; chỉ tăng minor/major khi người dùng yêu cầu rõ.
- Mọi bản phát hành phải nằm tại `D:\1\Cambida\release\<version>\`.
- Không tạo lại các thư mục `release_2_*` ở root project.
- `build_*`/`dist_*` theo phiên bản chỉ là tạm; sau khi đóng gói và kiểm tra xong phải xóa, chỉ giữ artifact cuối trong `release\<version>`.
