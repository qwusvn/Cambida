# Cambida v2.4.0 — Âm thanh xem lại H.265 trên trình duyệt

- Tích hợp phát âm thanh (AAC) đồng bộ cho chế độ xem lại H.265 trực tiếp trên trình duyệt bằng audio-only MP4 Blob.
- Giữ nguyên luồng âm thanh gốc, không chuyển mã (transcode) ở server, không tốn thêm tài nguyên máy chủ.
- Bổ sung nút loa nhỏ (bật/tắt tiếng) trực tiếp trên khung video (replay & cut); mỗi clip bắt đầu tắt tiếng để không bị chặn bởi chính sách tự phát của Safari/iOS.
- Âm thanh tự động đồng bộ theo tiến trình video, hỗ trợ tua, tạm dừng và thay đổi tốc độ (0.5X, 1X, 2X, 4X) với cơ chế tự động bù độ lệch (drift correction).
- Tiếp tục kế thừa đầy đủ bộ giải mã H.265 WebAssembly chạy trong Worker trên web cho iPhone/trình duyệt không hỗ trợ HEVC native.

## Giới hạn cần biết

- Clip không có luồng âm thanh sẽ tự động ẩn nút loa và phát không tiếng.
- Yêu cầu người dùng chạm nút loa để kích hoạt âm thanh lần đầu trên Safari/iOS (theo chính sách Autoplay của Apple).
- Bản phát hành đầy đủ không chứa cấu hình, token, cơ sở dữ liệu hoặc video của máy sử dụng.

## Kiểm chứng

- 312 kiểm thử Python, 6 subtest và 40 kiểm thử Node đạt 100%.
- Kiểm thử đồng bộ audio/video trên trình duyệt thực tế đạt độ lệch ~48ms ở 1X và ~89ms ở 2X.
- Gói được kiểm tra toàn bộ manifest hash, asset decoder và nguồn LGPL.
