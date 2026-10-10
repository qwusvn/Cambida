# Cambida 2.3.5 — updater độc lập và dọn code

Gói cuối: `D:/1/cambida/release/2.3.5.zip` (229653352 bytes).
SHA-256: `73bcf8dcf1d1b06288caaf2e60f68afc473293aee05762c49eb719f633f0b16b`.
Báo cáo: `release/2.3.5.validation.json`; build exit 0.

## Thay đổi

- Updater độc lập: dừng tiến trình của đúng thư mục cài đặt, tải ZIP, kiểm tra manifest, ghi đè theo `update_exclude.txt`, mở lại ứng dụng.
- Xóa source watchdog và updater CMD/PowerShell cũ cùng test watchdog đã retired. Cầu nối `updater.ps1` của bản 2.3.x cũ được giữ để gọi updater mới.
- Xóa 7 hàm không còn nơi gọi trong `1.py`, gộp phần xử lý tên/đường dẫn/thời gian video, RTSP preview và IP LAN vào services; giảm 282 dòng ở `1.py`.
- Bỏ 16 import thư viện chuẩn không dùng trong camera modules; sửa socket tìm IP LAN để luôn đóng khi gặp lỗi.
- Gói native có 21 module, không còn `native_update`. Source `native_update.py` vẫn giữ cho pipeline camhl lịch sử đang tham chiếu nó.
- Bỏ thư mục `cloudflared_setup` và 6 file hướng dẫn/cài tunnel thủ công khỏi gói. Giữ `cloudflared.exe` để kết nối domain được ControlHub cấp; dữ liệu token cũ được bảo toàn.
- Giữ API camera, callback, route, khôi phục runtime state, cấu hình, media và các file bản sao chưa commit của người dùng.

## Kiểm tra

- 203 regression checks trước khi dọn gói: PASS, không skip trong nhóm đã chạy. Lần đóng gói lại chỉ đổi danh sách file và tài liệu, không đổi source runtime.
- 20 checks với updater EXE trong gói cuối: PASS, có tải ZIP qua HTTP cách ly, overlay và restart chương trình thử.
- Nạp đủ 21 module từ PYD của gói phát hành; thử helper video và `/api/ping`: PASS. Không khởi động background worker, camera hoặc mạng ngoài.
- 275 file manifest / 277 file ZIP / 39 file source snapshot khớp hash; CRC, version 2.3.5 và metadata EXE: PASS. ZIP không còn `cloudflared_setup`.
- Git diff check và cú pháp Python: PASS.
- Camera, tunnel và GUI thật: chưa kiểm thử. Cầu nối PowerShell legacy: chưa xác nhận do timeout khởi động PowerShell trên máy này.

## Bảo toàn

- Source trước tối ưu: `staging/before-optimization-20261010-212219`.
- Gói nháp trước tối ưu: `D:\1\cambida\release\archive\2.3.5-before-optimization-20261010-213113`.
- Gói trước khi dọn bộ cài tunnel: `D:\1\cambida\release\archive\2.3.5-before-package-cleanup-20261010-220018`.
- Không triển khai vào thư mục chạy, không commit/tag hoặc đăng GitHub.
- Build dùng Python 3.13 hiện có với Cython 3.1.6 trên D: vì Python redirector trong venv bị treo trước biên dịch; không cài thêm thư viện hệ thống.
