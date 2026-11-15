# Cambida v2.3.9 — Xem lại H.265 trên trình duyệt

- Tích hợp bộ giải mã HEVC/H.265 WebAssembly chạy trong Worker ngay trên trang xem lại.
- Dùng bộ giải mã mới mặc định cho bản ghi local; giữ timeline, tua, tốc độ và chuyển clip.
- Giao diện gọn, không còn nút chọn decoder và nút phát/tạm dừng overlay.
- Bổ sung go2rtc WebRTC live preview và MJPEG fallback; API/RTSP sidecar chỉ nghe loopback, proxy kiểm tra camera/quyền riêng tư.
- Gói ZIP đầy đủ có runtime, bộ giải mã, giấy phép MIT/LGPL và nguồn/hướng dẫn dựng lại tương ứng.
- Giữ dữ liệu/cấu hình máy hiện có; không chứa config, token, database hoặc video của quán.

## Giới hạn cần biết

- Decoder phần mềm hiện không phát âm thanh; chỉ hỗ trợ MP4 H.265 local hoàn tất, tối đa 32 MiB mỗi clip.
- NVR dùng đường phát hiện có; không cam kết WASM cho NVR hoặc H.264.
- Người dùng đã xác nhận xem H.265 mượt trên iPhone. Đây không phải bảo đảm hiệu năng cho mọi thiết bị.

## Kiểm chứng

- 312 kiểm thử Python, 6 subtest và 37 kiểm thử Node đạt.
- Kiểm thử bridge dùng updater.exe được tạo bởi chính pipeline hiện tại, không dùng binary cũ ở root.
- Gói được kiểm tra toàn bộ manifest hash, asset decoder và nguồn LGPL, không có dữ liệu runtime riêng.
