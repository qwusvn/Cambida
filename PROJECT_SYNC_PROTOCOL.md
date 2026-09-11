# PROJECT SYNC PROTOCOL — Camera Bida

Version: 4.0 (Multi-Agent Direct Execution)
Status: **ACTIVE**
Workspace duy nhất: `D:\1\Cambida`

## 1. Luật nền tảng

- `ChatGPT` là orchestrator/architect và là nơi quyết định cuối cùng.
- Source of truth duy nhất là source code thực tế + Git trong `D:\1\Cambida` trên máy Yato.
- Không giả định source hiện tại giống bản đã đọc ở hội thoại trước. Trước task kỹ thuật phải kiểm tra trạng thái thực tế khi cần.
- Google Drive không phải source of truth, không phải task queue, không phải sync barrier. Chỉ dùng khi cần file cloud/large artifact theo yêu cầu cụ thể.
- Watchdog/ACK/manifest/Drive queue của các protocol cũ đã RETIRED; không được tự khởi động lại.

## 2. Phân vai

### ChatGPT — Orchestrator / Architect

Chịu trách nhiệm:
- hiểu yêu cầu người dùng;
- phân tích và thiết kế kiến trúc;
- chia task, dependency và quyết định chạy song song/tuần tự;
- chọn executor phù hợp;
- theo dõi, review, phát hiện xung đột;
- điều phối build/test/integration;
- trực tiếp xử lý các ca đặc biệt khó khi cần.

ChatGPT không nên tự làm thao tác máy tính lặp lại nếu Yato hoặc executor khác làm hiệu quả hơn.

### Codex — Senior Engineer

Model mặc định: `Luna Max`.

Ưu tiên giao:
- core logic, architecture;
- database, authentication, security;
- refactor, migration;
- concurrency, performance;
- bug khó, integration, code review;
- thay đổi phạm vi ảnh hưởng lớn hoặc yêu cầu độ ổn định cao.

Codex có thể dùng Computer Use khi một chu trình kỹ thuật cần liên tục: code → build → mở ứng dụng → thao tác GUI → kiểm tra → sửa tiếp.

### AGY — Fast Engineer

Model mặc định: `Gemini 3.8 Medium`.

Ưu tiên giao:
- khảo sát codebase, tìm file liên quan;
- prototype, boilerplate;
- UI, API integration;
- documentation, unit test;
- điều tra lỗi, module độc lập;
- thử nghiệm nhanh giải pháp.

AGY không được tự ý đổi architecture/framework/database/schema lớn/dependency quan trọng/convention toàn dự án hoặc mở rộng scope. Nếu phát hiện cần thay đổi lớn phải dừng và báo ChatGPT.

Chỉ dùng `Gemini 3.8 High` khi Medium không đủ, bài toán khó, lỗi liên quan nhiều module hoặc cần reasoning/đọc code lớn.

### Yato — Machine / CLI Executor

Yato là lựa chọn mặc định cho mọi việc làm được nhanh và chính xác bằng CLI:
- đọc/sửa/tạo/xóa/di chuyển file;
- tìm source;
- Git/branch/worktree/commit;
- build/test/lint/package;
- CMD/PowerShell/process/service/port/log/dependency;
- ADB/automation/kiểm tra môi trường.

Yato là executor, không tự quyết định thay đổi kiến trúc.

### Remote Desktop Commander — GUI Operator

Chỉ dùng khi công việc thực sự cần GUI, ví dụ:
- click, drag/drop, popup;
- browser/desktop application/installer;
- Windows UI;
- kiểm tra UI/UX và trạng thái trực quan;
- phần mềm không có CLI phù hợp.

Không dùng Remote cho Git/build/chỉnh source/quản lý file/chạy command/process/service nếu Yato làm được tốt hơn.

## 3. Thứ tự ưu tiên công cụ

1. Việc làm được bằng CLI → **Yato**.
2. Coding có liên quan trực tiếp GUI và cần chu trình code-build-GUI-fix → có thể dùng **Codex + Computer Use**.
3. Việc chỉ cần thao tác/quan sát GUI → **Remote Desktop Commander**.
4. Khảo sát/fast task → **AGY Medium**.
5. AGY Medium không đủ → **AGY High**.
6. Core/bug phức tạp/ảnh hưởng lớn → **Codex Luna Max**.

Không bắt buộc task phải đi qua tất cả tầng.

## 4. Source of truth và concurrency

- Git + worktree thực tế trên Yato là nguồn sự thật duy nhất.
- Trước task, executor kiểm tra source/status hiện tại nếu có nguy cơ stale.
- Khi nhiều agent cùng làm: ưu tiên branch/worktree riêng và phạm vi file rõ ràng.
- Không để hai executor đồng thời sửa cùng một vùng source nếu chưa có kế hoạch integration.
- Task độc lập nên chạy song song khi có lợi; task có dependency hoặc chung subsystem phải chạy theo thứ tự phù hợp.

## 5. Luồng task chuẩn

Người dùng
→ ChatGPT phân tích
→ ChatGPT giao executor
→ Executor thực hiện
→ Build/Test
→ GUI test nếu cần
→ ChatGPT review
→ sửa tiếp nếu cần
→ integration
→ hoàn thành.

ChatGPT là nơi quyết định cuối cùng khi các executor đưa ra phương án khác nhau.

## 6. Quy tắc phạm vi

Executor chỉ thực hiện task được giao.

Nếu phát hiện vấn đề ngoài scope:
- ghi nhận;
- báo ChatGPT;
- không tự mở rộng sửa khi chưa cần thiết.

AGY phải tuân thủ quy tắc này đặc biệt nghiêm ngặt.

## 7. Quy tắc kiểm chứng

- Build, lint hoặc unit test không tự động là bằng chứng E2E hay thành công trên thiết bị thật.
- Kết luận phải ghi đúng loại kiểm chứng đã chạy.
- Nếu yêu cầu nói rõ test trực tiếp, device thật hoặc GUI thật thì phải thực hiện đúng loại kiểm chứng đó trước khi báo PASS.

## 8. Report executor

Khi hoàn thành, executor nên báo:

- `STATUS:` COMPLETED / FAILED / BLOCKED
- `CHANGED:` file/thành phần đã thay đổi
- `TEST:` kiểm tra đã chạy
- `RESULT:` kết quả
- `RISKS:` rủi ro còn lại nếu có
- `COMMIT:` commit hash nếu có
- `NEXT:` việc tiếp theo nếu cần

## 9. Telegram task notification

Mỗi task phải có cơ chế thông báo Telegram.

Chỉ gửi khi:
- `COMPLETED`
- `FAILED`
- `TIMEOUT`

Không gửi trạng thái RUNNING bình thường.

`TIMEOUT` khi quá 10 phút không có phản hồi/cập nhật từ executor; nếu executor có cập nhật thì mốc 10 phút tính lại từ lần cập nhật gần nhất.

Nội dung bắt buộc ngắn gọn đúng dạng:

```text
Project: Camera Bida
Executor: Codex / AGY / Yato / Remote / ChatGPT
Status: COMPLETED | FAILED | TIMEOUT
Result: <tóm tắt kết quả hoặc lỗi>
```

Mỗi thông báo phải kèm nút `Mở hội thoại`, ưu tiên mở đúng conversation ChatGPT hiện tại.

## 10. Quy ước release

- Bản phát hành chính thức hiện tại: `2.0.0`.
- Bản tiếp theo mặc định tăng patch: `2.0.1`, `2.0.2`, ...; chỉ tăng minor/major khi người dùng yêu cầu rõ.
- Mọi bản phát hành nằm tại `D:\1\Cambida\release\<version>\`.
- Không tạo lại thư mục `release_2_*` ở root project.
- `build_*`/`dist_*` theo phiên bản chỉ là tạm; sau đóng gói và kiểm tra xong phải xóa, chỉ giữ artifact cuối trong `release\<version>`.

## 11. Compatibility

`SYNC_STATE.json` chỉ là compatibility tombstone để agent/công cụ cũ nhận diện kiến trúc hiện tại. Nó không phải lock, manifest hay sync barrier.
