# Cambida 2.4.2 — Sửa live màn hình đen, phát lại hai codec

- Sửa màn hình đen khi xem trực tiếp: hiển thị đúng video WebRTC hoặc ảnh MJPEG dự phòng; live video-only tắt tiếng mặc định để Chrome cho phép tự phát.
- Tự nhận diện bản ghi H.264/H.265: H.264 dùng trình phát gốc với nút âm thanh, H.265 dùng bộ giải mã WASM tự lưu trữ.
- Khôi phục bộ giải mã xem lại khi chuyển từ live về bản ghi.
- Nâng giới hạn bộ đệm bản ghi H.265 từ 32 MB lên 1 GB và giảm sao chép bộ nhớ khi chuẩn bị âm thanh. Giới hạn này không bảo đảm thiết bị ít RAM phát được clip 1 GB.
- Giữ nguyên cơ chế bảo vệ thoát ứng dụng và tự chạy khi đăng nhập của 2.4.1.

## Gói cập nhật

Gói đầy đủ `2.4.2.zip`, manifest schema 2. Không chứa cấu hình quán, thông tin đăng nhập, database, token, video hoặc log. Không tự cập nhật máy quán trong lần phát hành này.

## Phạm vi kiểm chứng

Live WebRTC, MJPEG dự phòng và chuyển về xem lại H.265 đã được kiểm tra trực tiếp trên máy Quán 89. H.264 được kiểm tra bằng fixture; chưa kiểm tra lại trên iPhone hoặc mọi loại camera.
