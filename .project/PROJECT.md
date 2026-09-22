# PROJECT — Cambida

Workspace duy nhất: `D:\1\Cambida`.

## Mục tiêu
Phát triển và duy trì Cambida/CCTV hiện có, bảo toàn hành vi đã chốt; ưu tiên tính ổn định và vận hành thực tế.

## Thứ tự ưu tiên và nguồn sự thật
- Yêu cầu mới nhất của người dùng > quy tắc riêng dự án > Source/Git > `.project` > CodeGraph > quy tắc điều phối toàn cục > bộ nhớ hội thoại.
- Source/Git là nguồn sự thật kỹ thuật; `.project` là bộ nhớ vận hành; CodeGraph cung cấp ngữ cảnh, không tự ghi đè sự thật kỹ thuật.
- Nguồn quy tắc điều phối hiện hành cho ChatGPT: `D:\1\gptagycodex.md`, `SPEC_VERSION=2026-09-22.1` tại thời điểm đồng bộ. Bản sao `D:\1\Cambida\gptagycodex.md` chỉ là tài liệu tham khảo do người dùng yêu cầu, không thay thế bản toàn cục.
- Quy tắc này chỉ áp dụng khi người dùng làm việc qua ChatGPT. Các phiên AGY/Codex độc lập thực hiện theo chỉ thị trực tiếp của người dùng trong ứng dụng tương ứng.

## Ràng buộc riêng Cambida
- Chỉ làm việc trong dự án hiện tại; không sửa cấu hình camera, media, dữ liệu runtime, source, release hoặc server đang chạy ngoài phạm vi được duyệt.
- Bảo toàn task đang chạy, trạng thái Git hiện hành, tài liệu/mã nguồn dirty từ trước và tài nguyên chia sẻ.
- Chỉ stage hoặc commit tệp và phần thay đổi thuộc task; không đưa các thay đổi cũ khác vào commit.
- Chỉ đóng gói PyInstaller hoặc phát hành khi người dùng yêu cầu rõ ràng. Không tự chạy test, regression, build dùng để kiểm thử hoặc thử thiết bị/emulator khi chưa có yêu cầu và xác nhận kiểm thử.
- Build/lint/unit test không được gọi là kiểm thử đầu cuối trên thiết bị thật.
- Không tự khôi phục protocol Drive queue/manifest/sync barrier/ACK đã ngừng sử dụng.

## Bộ nhớ dự án
Khi đã được phép kiểm tra task: đọc `PROJECT.md` → `DECISIONS.md` → `STATE.md` → `TASKS.md` → `HANDOFF.md` trong phạm vi liên quan, rồi đối chiếu source/Git và các task còn hoạt động.

## Ràng buộc phát hành sản phẩm
- Trạng thái phát hành hiện hành phải đối chiếu source/Git và `.project/STATE.md`; không xem mốc 2.1.0 trong các ghi chú cũ là trạng thái production mới nhất.
- Không nâng lên dòng 3.x chỉ dựa vào unit test khi còn thiếu bằng chứng truyền media NetSDK thực tế, gồm phiên xác thực hợp lệ, khung hình/snapshot và tệp MP4 có thể xác minh.
- Khi có yêu cầu đóng gói, giữ PyInstaller onedir, HTML/FFmpeg sidecar và các thư viện NetSDK đúng theo spec của phiên bản được duyệt; bảo toàn `config.json`, media và dữ liệu runtime.
