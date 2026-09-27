# ARCHITECTURE — Cambida / Camera Highlight

Cập nhật chuẩn hóa: 2026-09-27 (Áp dụng từ phiên bản 2.2.2 trở đi)

## 1. Cây thư mục phát hành chuẩn (Canonical Release Tree)

Theo quyết định chốt ngày 2026-09-27 của người dùng, toàn bộ các bản đóng gói phát hành và cập nhật của Cambida / Camera Highlight bắt buộc tuân thủ 100% cấu trúc cây thư mục chuẩn sau:

```text
<RELEASE_ROOT>/
├── camhl.exe                 # File thực thi ứng dụng chính (cố định tên, onedir, noconsole)
├── updater.exe               # Trình cập nhật nhị phân độc lập (onefile, noconsole)
├── ffmpeg.exe                # Nhị phân FFmpeg phục vụ ghi hình, cắt ghép video
├── cloudflared.exe           # Nhị phân Cloudflare Tunnel
├── index.html                # Giao diện web xem lại / cắt video
├── admin.html                # Giao diện web quản trị hệ thống
├── admin_login.html          # Giao diện web đăng nhập quản trị
├── home.html                 # Giao diện web trang chủ / công tắc riêng tư
├── live_all.html             # Giao diện web xem trực tiếp toàn bộ camera
├── stats.html                # Giao diện web thống kê
├── timeline.html             # Giao diện web timeline
├── RELEASE_VERSION.txt       # File text chứa số phiên bản hiện tại (VD: 2.2.2)
├── VERSION.txt               # File text tương thích ngược số phiên bản
├── release_manifest.json     # Bảng mã băm SHA-256 đối soát toàn vẹn file
├── update.json               # Metadata phục vụ tự động cập nhật
└── _internal/                # Thư mục runtime phụ trợ (Python 3.13, NetSDK DLLs, hạt giống cấu hình)
    ├── vendor/dahua_netsdk/  # 10 DLLs SDK Dahua (avnetsdk, dhnetsdk, dhplay, ...)
    └── config.release.json   # Hạt giống cấu hình sạch (rỗng camera, không chứa mật khẩu cá nhân)
```

## 2. Các nguyên tắc kiến trúc bắt buộc

1. **Tên nhị phân cố định `camhl.exe`**:
   - Tuyệt đối không sinh file thực thi mang tên theo phiên bản (như `CCTV_2.2.2.exe`, `Cambida.exe`) trong các lần build/update.
   - Giúp mọi shortcut trên Desktop, Windows Startup và các script hệ thống luôn ổn định mà không bao giờ bị đứt gãy sau mỗi lần nâng cấp.

2. **Trình cập nhật 100% nhị phân `updater.exe`**:
   - Thay thế toàn bộ kịch bản `.cmd` và `.ps1` (`updater.cmd`, `native_updater.ps1`).
   - Loại trừ hoàn toàn lỗi hỏng mã hóa ký tự Windows (ANSI/CP1252 vs UTF-8) và rào cản PowerShell Execution Policy.
   - Tự động bảo vệ dữ liệu vận hành: không bao giờ ghi đè `config.json`, `analytics.db`, `device_id.key`, `tunnel_token.txt`, `logs/`, `cctv_videos/`, `nvr_cache/`.
   - Tự động dọn dẹp các tệp cũ lỗi thời (`run.cmd`, `Chay_CCTV.cmd`, `cloudflared_setup/`, `CCTV_*.exe`).

3. **Tích hợp toàn bộ vào `camhl.exe`**:
   - Không dùng thư mục `cloudflared_setup\`. `camhl.exe` tự khởi chạy tiến trình con `cloudflared.exe` ở chế độ ngầm và giám sát sự sống.
   - Không dùng các file kịch bản khởi động ngoài (`run.cmd`, `Chay_CCTV.cmd`). `camhl.exe` tự đăng ký khởi động cùng Windows qua registry (`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`) và hiển thị icon trên khay hệ thống (System Tray).

4. **Quy tắc Zero Config Pollution**:
   - Mọi bản phát hành đều phải vượt qua bài kiểm tra đối soát bảo mật (Security Audit).
   - Tuyệt đối cấm đưa `config.json` cá nhân, mật khẩu camera, token riêng tư, video media, nhật ký logs hoặc tệp mã nguồn thô `.py` vào gói phân phối.
