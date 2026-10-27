# CAMBIDA — OPENCODE PROJECT INSTRUCTIONS

Hướng dẫn dự án dành cho phiên OpenCode làm việc trong `D:\1\cambida`.
Tài liệu độc lập của AGY (`.agy/instructions.md`) và Codex (`AGENTS.override.md`, `.codex/instructions.md`) được giữ nguyên cho phiên độc lập tương ứng; OpenCode không sử dụng chúng làm quy chuẩn điều phối.

## 1. Điều phối & Quy tắc chung
- Tuân thủ phiên làm việc hiện hành và quy chuẩn toàn cục tại `C:\Users\qwusv\.config\opencode\AGENTS.md` về routing, worker/reviewer theo rủi ro, và cổng phê duyệt.
- Quản lý bộ nhớ dự án theo **Project Memory V3** (`.project/INDEX.md` + `.project/NOW.md`): Coordinator là bên duy nhất ghi nhận checkpoint (sole writer) kèm kiểm tra revision. Tuyệt đối không nạp `.agy/`, `.codex/` hoặc `D:\1\gptagycodex.md` làm luật điều phối OpenCode.
- Tuân thủ nghiêm ngặt nguyên tắc **One-File Ownership** khi giao task cho subagent/worker.

## 2. Workspace & CodeGraph
- Workspace duy nhất: `D:\1\cambida`. Mọi thao tác source, Git, build, package và status phải thuộc workspace này.
- Nếu có `.codegraph/`: Ưu tiên sử dụng `codegraph_explore` (hoặc `codegraph explore`) để tra cứu cấu trúc, luồng code và dependency trước khi khảo sát diện rộng. Không tự ý khởi tạo nếu chưa có yêu cầu.

## 3. Ràng buộc kiểm thử & Thiết bị (Mặc định chỉ kiểm tra tĩnh)
- Dự án có tích hợp phần cứng/thiết bị camera và streaming: **Mặc định CHỈ sử dụng kiểm tra tĩnh** (static check, read-back, syntax check, diff review).
- **Tuyệt đối KHÔNG tự ý mở rộng quyền hạn**: Chỉ chạy test, regression, build phục vụ kiểm thử, can thiệp thiết bị thật/Android hoặc kiểm thử trình duyệt khi người dùng đã yêu cầu rõ ràng hoặc phạm vi công việc được phê duyệt bao gồm kiểm thử đó.
- Không mô tả kết quả build, lint hay unit test như là kiểm thử đầu cuối trên thiết bị thật.

## 4. Bảo vệ Dịch vụ & Dữ liệu
- Tuyệt đối không tự ý sửa đổi cấu hình camera, dữ liệu media, release artifact, mã nguồn ngoài phạm vi hoặc can thiệp dịch vụ đang chạy.
- Không tự tiện kill tiến trình, reset trạng thái, stage/commit thay đổi ngoài phạm vi được giao.
- **Giao thức cũ**: Toàn bộ cơ chế Drive queue/manifest/sync barrier/ACK protocol đã chính thức nghỉ hưu (retired); tuyệt đối KHÔNG tự tái kích hoạt.

## 5. Lưu trữ APK
- Mọi bản build APK hợp lệ (khi được yêu cầu) bắt buộc xuất bản tập trung qua script dùng chung: `D:\1\publish-latest-apk.ps1 -SourceApk "<đường-dẫn-APK-mới>" -ProjectName "cambida"` để ghi đè vào duy nhất `D:\apk\cambida.apk`.
- Duy trì đúng **1 file APK duy nhất** đại diện cho bản build mới nhất của dự án, không tạo hậu tố phiên bản gây phân mảnh.
