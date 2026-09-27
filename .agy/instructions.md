# CAMBIDA — AGY STANDALONE RULES

Phạm vi: chỉ áp dụng cho AGY khi người dùng chạy AGY trực tiếp trong workspace `D:\1\cambida`.

Không áp dụng tài liệu này cho ChatGPT + YATO hoặc Codex. AGY standalone không lấy `D:\1\gptagycodex.md` làm nguồn điều phối.

Workspace duy nhất: `D:\1\cambida`.
Bộ nhớ vận hành chung của dự án: `.project/`.

## 1. Thứ tự ưu tiên

`USER_LATEST > AGY_RULES > PROJECT_SPECIFIC > SOURCE_GIT > PROJECT_MEMORY (.project) > CODEGRAPH > OLD_CHAT_MEMORY`.

Source/Git là sự thật kỹ thuật. `.project` là bộ nhớ vận hành, không phải luật điều phối của AGY. Nếu `.project` chứa tham chiếu tới `D:\1\gptagycodex.md`, coi tham chiếu đó là thông tin dành cho phiên ChatGPT trừ khi người dùng hiện tại nói rõ ngược lại.

## 2. Cổng xác nhận

Trước yêu cầu sửa đổi, triển khai, cấu hình, tạo file hoặc thực thi can thiệp hệ thống:
- Nêu phạm vi, kết quả dự kiến và kế hoạch thực hiện.
- Nêu các file dự kiến sửa/tạo khi xác định được.
- Chờ người dùng xác nhận rõ ràng như `ok`, `làm đi`, `triển khai`.

Nếu người dùng đã xác nhận đề xuất ngay trước đó thì không hỏi lại.

COMMA BYPASS: yêu cầu bắt đầu hoặc kết thúc bằng dấu phẩy (`,`) cho phép thực thi ngay. Dấu phẩy ở giữa câu không kích hoạt bypass.

Yêu cầu chỉ đọc như đọc file, review, xem log, Git status hoặc process status được phép thực hiện ngay.

## 3. Thực thi và phạm vi

- Chỉ làm việc trong `D:\1\cambida`, trừ khi người dùng đổi phạm vi rõ ràng.
- Trước khi ghi, kiểm tra source/Git và công việc đang dở liên quan để tránh ghi đè hoặc nhân đôi.
- Không tự coi chỉ dẫn dành riêng cho ChatGPT/YATO hoặc Codex là luật của AGY.
- CodeGraph, nếu có, chỉ cung cấp ngữ cảnh/phụ thuộc; phải đối chiếu với source/Git trước khi kết luận.
- Không tự reset, xóa, stage hoặc commit thay đổi ngoài phạm vi.
- Không kill tiến trình/dịch vụ không thuộc tác vụ.
- Nếu có nhiều worker cùng làm, không để hai worker ghi đồng thời lên cùng file hoặc tài nguyên dùng chung.

## 4. Kiểm thử

Chỉ chạy test, regression, build dùng để kiểm thử, thiết bị thật/Android hoặc kiểm thử trình duyệt khi người dùng đã yêu cầu hoặc phạm vi đã được xác nhận rõ ràng bao gồm việc kiểm thử đó.

Read-back, diff review, kiểm tra tồn tại file và kiểm tra cú pháp tĩnh không được tính là test.

Khi được phép test, bắt đầu từ phạm vi nhỏ nhất phù hợp rồi mở rộng khi cần.

## 5. Hoàn tất

- Review thay đổi thực tế trước khi báo hoàn tất.
- Chỉ báo hoàn tất khi đầu ra yêu cầu đã tồn tại và không còn hạng mục bắt buộc đang bị bỏ dở.
- Cập nhật `.project` khi phù hợp với mốc vận hành của dự án, nhưng không biến `.project` thành bản sao luật điều phối của AGY.
- Không sửa `D:\1\gptagycodex.md` từ phiên AGY standalone.

## 6. Ràng buộc riêng của dự án

- Không tự sửa cấu hình camera, dữ liệu media, release, dịch vụ đang chạy hoặc source ngoài phạm vi đã duyệt.
- Không stage hoặc commit thay đổi ngoài phạm vi; không reset hoặc kill tiến trình của task khác.
- Build, lint hoặc unit test không được mô tả như kiểm thử đầu cuối trên thiết bị thật.
- Protocol Drive queue/manifest/sync barrier/ACK cũ đã retired; không tự tái kích hoạt.
