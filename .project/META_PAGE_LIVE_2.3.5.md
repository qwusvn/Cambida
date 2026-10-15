# Cambida 2.3.5 — Meta Fanpage Live Video API (2026-10-14)

## Mục tiêu & trạng thái

Tích hợp **tạo/phát/dừng bài Live Fanpage bằng Meta Graph API** vào modal Facebook trên trang khách `/replay/cam<ID>`. Không thay thế chức năng phát Stream Key tĩnh/personal hiện có nếu chưa cấu hình Meta. Hoạt động trong source code 2.3.5; **chưa kết nối Meta thật, chưa deploy, chưa đóng gói hoặc commit**.

Mã nguồn:
- `services/meta_page_live.py`: client Graph API qua HTTPS tới host cố định `graph.facebook.com`, `POST /v26.0/{page_id}/live_videos` kèm `status=LIVE_NOW`, nhận `id` + `secure_stream_url`, lấy `permalink_url` qua GET nếu cần và gọi `POST /v26.0/{live_video_id}` kèm `end_live_video=true`.
- `services/facebook_live.py`: ưu tiên Meta Page API khi đủ Page ID/Token; mỗi lần Live được một ingest RTMPS riêng; gọi FFmpeg hiện có; quản lý 6 camera đồng thời (giới hạn phần mềm), tối đa theo cấu hình 5–240 phút; kiểm tra riêng tư và ngắt bài Live qua Graph ngay cả khi encoder tắt hoặc lỗi. Nếu Meta chưa xác nhận dừng, không giải phóng phiên cho Live mới; công cụ tiếp tục thử lại. Hợp đồng chế độ Stream Key tĩnh/cá nhân vẫn giữ.
- `1.py`: cung cấp Page ID/Token cục bộ cho client, không truyền token tới template khách và xóa token khỏi GET `/api/admin/config`; PUT giữ token cũ khi ô mới bỏ trống.
- `admin.html`: Page ID, Page Access Token và điều khiển cho phép/thu hồi phiên Live từng camera.
- `index.html`: ưu tiên tab Fanpage khi Meta API đã cấu hình, nút "Bắt đầu Live Fanpage"; hiển thị link bài Live khi Meta trả về, không yêu cầu khách mở Facebook Producer.

## Cách cấu hình (quản trị viên)

1. Mở **Admin → Facebook Live**, bật quyền phát Facebook Live cho khách.
2. Nhập **Page ID số** và **Page Access Token** hợp lệ trong giao diện quản trị, sau đó lưu cấu hình (chỉ máy quán). **Không gửi Page Token hoặc Stream Key vào chat**. Token cũng có thể được cung cấp cho tiến trình bằng biến môi trường `CAMBIDA_FACEBOOK_PAGE_TOKEN`, khi đó token env được ưu tiên hơn token lưu trong config.
3. Page phải thuộc quyền quản trị viên cấp token và đủ điều kiện livestream. App của Meta phải có quyền API thực tế phù hợp (có thể cần App Review/Advanced Access); **Development Mode và Page Token không bảo đảm API sẽ chấp nhận trong sản xuất**. Token không "vĩnh viễn" theo nghĩa không thể thu hồi.
4. Nhân viên nhập **số camera** trên trang Admin và bấm "Cho phép Live bàn này" để mở cửa sổ 20 phút, dùng một lần. Đây là bước chống việc một người có QR cố định tự đăng Live lên Fanpage khi quán chưa cho phép.
5. Khách quét QR → HTTPS → modal Live Facebook → Fanpage quán → tích đồng ý → **Bắt đầu Live Fanpage**. Web sẽ tự tạo buổi Live bằng Meta, sau đó FFmpeg gửi nguồn camera và có thể hiện link bài Live để chia sẻ.
6. Khách bấm "Dừng", hoặc nhân viên bấm "Thu hồi / Dừng Live" → dừng FFmpeg + yêu cầu Meta kết thúc bài Live.

## Bảo mật/giới hạn

- Backend chỉ cho phép URL ingest RTMPS thuộc tên miền Meta; không nhận host RTMP tùy ý từ khách ở chế độ Page API.
- Page Token không gửi trong API JSON, URL hoặc tham số FFmpeg; bảo vệ bằng cơ chế cấu hình máy quán. **Nếu lưu trong config.json thì hiện ở dạng văn bản**: cần bảo vệ ACL/backup. Khuyến nghị dùng biến môi trường hoặc kho bí mật OS khi triển khai diện rộng.
- Quyền bắt đầu: guest session + nonce CSRF + HTTPS + consent, giới hạn bàn đang hoạt động/riêng tư, và staff-issued 1-time approval. Người có link QR vẫn có thể cố giành quyền khi nhân viên đã bật cho bàn; mức bảo vệ mạnh hơn cần xác thực người sử dụng bàn thực tế.
- Đường link post chỉ xuất hiện nếu Meta trả permalink hợp lệ; không tự bịa permalink từ Page ID. Meta quyết định thời điểm bài được công khai, điều kiện Page, khu vực, quyền publish và khả năng tải lại VOD.
- Khách không phải Facebook-login; mọi Live lên Fanpage **của quán**, không phải profile cá nhân của khách.
- Quản lý phiên và phê duyệt là **in-memory**, giả định Waitress 1 process. Nếu chạy nhiều worker process thì cần shared store/lock.
- `services/meta_page_live.py` sẽ được `scripts/build_native.py` tự phát hiện khi build native (quét các module trong `services`), nhưng **chưa chạy build/tạo gói 2.3.5 mới**.

## Kiểm thử cách ly

- `tests/test_meta_page_live.py`: mô phỏng Graph API, FFmpeg, phiên khách và quản trị viên; tạo Live riêng từng bàn, link, kết thúc, lỗi quyền/token, nguồn ingest không hợp lệ, không lộ token qua GET Admin, staff approval một lần, retry stop Meta.
- `tests/test_facebook_live.py` và các test camera/auth/privacy cũ vẫn là regression.
- Chỉ dùng dữ liệu giả, **không tạo bài Facebook thật** và không phát camera thật.

## Yêu cầu trước phát hành

- Dùng Page ID/Token **thật** từ Fanpage thử nghiệm, được quản trị viên cấp quyền để xác minh Graph API `LIVE_NOW`, permalink, stop API, trạng thái Live/VOD và độ trễ.
- Cần đo CPU/mạng cho số luồng 720p đồng thời dự kiến, kiểm thử RTSP và gói native 2.3.5, rồi mới enable cho khách thật.
