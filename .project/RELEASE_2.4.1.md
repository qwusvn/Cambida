# Cambida 2.4.1

- Tự chạy nền khi đăng nhập Windows; mở bản mới một lần để đăng ký đúng đường dẫn cài đặt.
- Sửa khởi động tự động để dùng trực tiếp Cambida.exe, không phụ thuộc run.cmd cũ và không tự mở trình duyệt.
- Yêu cầu mật khẩu chủ cố định khi chọn Thoát ở khay hệ thống, độc lập mật khẩu config/quản trị.
- Nhập sai, hủy hoặc đóng hộp thoại không dừng ứng dụng; không có chức năng xem/đổi mật khẩu chủ trong app.
- Giữ nguyên luồng cập nhật tự động và khởi động lại; không thay đổi dữ liệu/cấu hình camera tại cửa hàng.

Lưu ý: bảo vệ lệnh Thoát của ứng dụng, không ngăn Task Manager hoặc tài khoản quản trị Windows kết thúc tiến trình. Tự chạy khi đăng nhập, không phải Windows Service trước đăng nhập.

Gói phát hành đầy đủ: `2.4.1.zip`. Giải nén đè vào thư mục cài đặt hiện tại hoặc dùng cập nhật tự động; dữ liệu vận hành không nằm trong ZIP.

Kiểm chứng: 323 bài pytest + 13 subtest, 40 bài Node đạt; đã kiểm tra trực quan hộp thoại mật khẩu Windows. Không triển khai lên máy cửa hàng hay chạy bộ ghi hình để kiểm thử.
