# Cambida 2.3.5 — Fanpage Facebook Live 1-touch & Stream Pool (2026-10-12)

## Scope

- Chế độ 1 chạm: Khách quét QR bàn → /replay/cam<ID> → mục **🔴 Phát trực tiếp lên Fanpage Quán**.
- Khách không cần nhập Stream Key hay Server URL: Stream Key được lấy từ kho luồng bảo mật của quán trên máy chủ.
- Giao diện khách: Tích đồng ý xác nhận hình ảnh và bấm **"🔴 Bắt đầu Live Bàn này"**.
- Khi phát thành công: Trình duyệt hiển thị trạng thái phát, nút mở trực tiếp Fanpage Facebook (`Xem bài Live trên Facebook ↗`) và nút Dừng phát.
- RTSP/NVR substream → FFmpeg H.264 720p/30fps + AAC silent → Facebook RTMPS (đẩy trực tiếp từ máy quán qua FFmpeg).

## Cơ chế Kho Luồng Nhàn Rỗi (Stream Key Pool)

- **Cấp phát luồng rảnh (Idle Slot Allocation)**: Quán cấu hình danh sách N Fanpage dự phòng (`facebook_page_pools`), mỗi Fanpage có Persistent Stream Key và Page URL riêng.
- Khi bàn nào bấm Bắt đầu Live trước, hệ thống tự động gán vào luồng Fanpage đang rảnh đầu tiên và trả về đúng link Fanpage của luồng đó.
- Các bàn có thể phát song song cùng lúc độc lập trên từng Fanpage riêng biệt.
- **Xử lý khi đầy tải**: Khi tất cả các luồng trong kho đều đang có bàn sử dụng, bàn tiếp theo bấm Live sẽ nhận được thông báo bận: *"Tất cả các luồng Fanpage của quán đều đang có bàn sử dụng. Vui lòng chờ bàn khác kết thúc."*
- **Tự động thu hồi slot**: Khi một bàn bấm Dừng phát (hoặc hết thời lượng tối đa / bàn bật riêng tư / tắt server), slot của luồng đó lập tức được giải phóng về trạng thái nhàn rỗi để bàn khác sử dụng ngay.

## Bảo mật

- **Bảo mật Stream Key**: Stream Key của các Fanpage được quản trị viên điền trên trang Admin (`/admin`), lưu trong `config.json` cục bộ của máy quán, tuyệt đối không gửi về trình duyệt khách qua bất kỳ API hay template nào.
- **Bảo vệ phiên và thời lượng**: Tự ngắt sau thời gian tối đa do quán cấu hình (mặc định 60 phút), hoặc khi bàn chuyển sang chế độ riêng tư / tắt camera, hoặc khi tắt server. Chủ phiên mới được dừng phát.

## Code

- `services/facebook_live.py`: Quản lý kho luồng `facebook_page_pools`, cấp phát luồng nhàn rỗi, thu hồi slot, hỗ trợ phát song song nhiều bàn, che giấu Stream Key, tự dọn dẹp FFmpeg.
- `1.py`: Đăng ký `_get_facebook_page_live_config` trích xuất `slots`, lưu cấu hình `facebook_page_pools` qua API Admin `/api/admin/config`.
- `index.html`: Giao diện khách 1 chạm, tự động nhận `page_url` của luồng được cấp phát để mở đúng bài live trên Facebook.
- `admin.html`: Giao diện quản lý Danh sách Khóa Luồng Fanpage (thêm/xóa từng luồng, tên Fanpage, Persistent Stream Key, Page URL).
- `tests/test_facebook_live.py`: Bộ kiểm thử 8/8 test PASS (1-touch mode, cấp phát xoay vòng nhiều luồng song song, chặn khi pool đầy, tự động cấp lại slot sau khi giải phóng, CSRF nonce, HTTPS, camera riêng tư).
