# CCTV Changelog

## 1.3.43 - 2026-09-09

- Hoàn thiện timeline đa camera với một trục thời gian chung, ruler giờ/phút, playhead, zoom, preset khoảng thời gian, marker sự kiện và modal phát lại Bootstrap.
- Bổ sung `GET /timeline` và `GET /api/timeline`, dùng chỉ mục `video_segments`/`video_events`, metadata tên file và filesystem stat; không dùng ffprobe hàng loạt.
- Bổ sung validation ISO local tối đa 24 giờ, bảo vệ basename MP4, fallback cho tên file legacy và test độc lập cho Timeline API/helper.
- Sao lưu trước khi sửa tại `backup/1.3.43/` gồm `1.py`, `timeline.html` và `CHANGELOG.md` bản gốc.

## 1.3.42 - 2026-08-24

- Đóng gói và chuẩn hóa phiên bản `CCTV_1.3.42.exe`.
- Đồng bộ giao diện Web, timeline, admin và công cụ ffmpeg.
- Dọn dẹp toàn bộ tệp thực thi, bản sao lưu và artifact trung gian của các phiên bản cũ.

## 1.3.11 - 2026-08-14

- Ghép video vẫn ưu tiên đúng khoảng đã chọn; nếu không có clip giao nhau, tự tìm clip gần nhất trong phạm vi một giờ.
- Khoảng ghép thay thế giữ nguyên độ dài người dùng chọn và hiển thị thông báo rõ trên tiến trình.
- Đóng gói phát hành: `CCTV_1.3.11.exe`.
- Sao lưu trước khi sửa: `backup/1.3.11/1_1.3.11.py.backup`, `index_1.3.11.html.backup` và `CHANGELOG_before_1.3.11.md`.

## 1.3.10 - 2026-08-09

- Bỏ ngoại lệ còn lại trong thư mục video: file `merge_list_*.txt` không còn bị xoá ngay sau khi ghép.
- Mọi xoá tự động trong thư mục video chỉ do bộ dọn dung lượng thực hiện khi tổng dung lượng vượt `disk_limit_gb`, theo thứ tự tệp cũ nhất.
- Việc admin tự bấm xác nhận xóa video ghép vẫn là thao tác thủ công riêng.
- Sao lưu trước khi sửa: `backup/1.3.10/1_1.3.10.py.backup` và `CHANGELOG_before_1.3.10.md`.

## 1.3.9 - 2026-08-09

- Khi chọn video xem lại, app đọc codec của chính tệp MP4 và hiển thị cảnh báo rõ ràng nếu là H.265/HEVC.
- Cảnh báo hướng dẫn tải file về xem bằng phần mềm phù hợp hoặc đổi camera sang H.264; không thay đổi luồng ghi hay phát video.
- Sao lưu trước khi sửa: `backup/1.3.9/1_1.3.9.py.backup`, `index_1.3.9.html.backup` và `CHANGELOG_before_1.3.9.md`.

## 1.3.8 - 2026-08-09

- Sửa ghép video qua nửa đêm: giờ kết thúc nhỏ hơn hoặc bằng giờ bắt đầu được hiểu là ngày hôm sau, cả ở giao diện lẫn API ghép.
- Giao diện xem lại hiển thị lỗi rõ ràng khi không tải được danh sách video.
- Giao diện ghép hiển thị lỗi rõ ràng khi kết nối SSE bị ngắt thay vì để thanh tiến trình dừng im lặng.
- Sao lưu trước khi sửa: `backup/1.3.8/1_1.3.8.py.backup`, `index_1.3.8.html.backup` và `CHANGELOG_before_1.3.8.md`.

## 1.3.7 - 2026-08-09

- Thống nhất tuyệt đối cơ chế xoá trong thư mục video: không tự xoá file tạm khi ghi lỗi.
- Tổng dung lượng và thứ tự dọn dẹp gồm mọi tệp thường trong thư mục video; chỉ khi vượt `disk_limit_gb` mới xóa tệp cũ nhất.
- Bổ sung log tên tệp bị xoá do đầy dung lượng.
- Sao lưu trước khi sửa: `backup/1.3.7/1_1.3.7.py.backup` và `backup/1.3.7/CHANGELOG_before_1.3.7.md`.

## 1.3.6 - 2026-08-09

- Bỏ cơ chế tự xoá video theo số ngày, gồm cả video đã ghép.
- Khôi phục cơ chế dọn dẹp của bản cũ: chỉ khi tổng dung lượng video vượt `disk_limit_gb` thì xóa tệp cũ nhất trước.
- Bỏ cấu hình và mục quản trị “Giữ video (ngày)” để không thể vô tình bật lại.
- Sao lưu trước khi sửa: `backup/1.3.6/1_1.3.6.py.backup`, `config_1.3.6.json.backup`, `admin_1.3.6.html.backup` và `CHANGELOG_before_1.3.6.md`.

## 1.3.5 - 2026-08-09

- Khôi phục nút `<` và `>` tại phần ghép video để chuyển lần lượt ngày trước/ngày sau.
- Vẫn giữ ô chọn ngày và định dạng hiển thị `Ngày: dd/mm/yyyy` như cũ.
- Không thay đổi phần ghi video, xem lại hoặc bản quyền.
- Sao lưu trước khi sửa: `backup/1.3.5/index_1.3.5.html.backup` và `backup/1.3.5/CHANGELOG_before_1.3.5.md`.

## 1.3.4 - 2026-08-09

- Khôi phục tin nhắn ghim Telegram làm nguồn bản quyền trung tâm: mỗi lần khởi động, app chỉ mở xem lại khi key hiện tại có trong tin ghim.
- Key được tính lại trực tiếp từ Windows `MachineGuid` trên máy đang chạy; cache, registry và identity cục bộ không thể cấp bản quyền khi bị copy sang máy khác.
- Lệnh `/activate` cũng xác minh key có trong tin nhắn ghim trước khi mở xem lại.
- Đóng gói phát hành: `CCTV_1.3.4.exe`.
- Sao lưu trước khi sửa: `backup/1.3.4/1_1.3.4.py.backup`.

## 1.3.3 - 2026-08-09

- Chuyển `cctv_license_cache.json`, `cctv_license_registry.json` và `cctv_device_identity.json` sang thư mục chứa EXE/`1.py`, không còn yêu cầu ổ D.
- Tự sao chép các file bản quyền cũ từ ổ D vào thư mục ứng dụng khi nâng cấp, giữ nguyên key/kích hoạt hiện có.
- Đóng gói phát hành: `CCTV_1.3.3.exe`.
- Sao lưu trước khi sửa: `backup/1.3.3/1_1.3.3.py.backup`.

## 1.3.2 - 2026-08-09

- Key máy được cố định tại `D:\cctv_device_identity.json`, không còn phụ thuộc thứ tự card mạng/Bluetooth sau khi khởi động lại.
- Khi nâng cấp máy đã kích hoạt, app giữ key trong cache cũ và cố định nó, không yêu cầu kích hoạt lại.
- Máy chưa kích hoạt tạo key từ Windows `MachineGuid`; chỉ dùng MAC làm phương án dự phòng khi không đọc được `MachineGuid`.
- Đóng gói phát hành: `CCTV_1.3.2.exe`.
- Sao lưu trước khi sửa: `backup/1.3.2/1_1.3.2.py.backup`.

## 1.3.1 - 2026-08-07

- Thời điểm kích hoạt mới lấy từ trường `message.date` của Telegram và lưu dạng UTC trong registry, thay vì dùng đồng hồ máy CCTV.
- `/listkey` hiển thị rõ thời điểm kích hoạt theo Telegram.
- Đóng gói phát hành: `CCTV_1.3.1.exe`.
- Sao lưu trước khi sửa: `backup/1.3.1/1_1.3.1.py.backup`.

## 1.3.0 - 2026-08-05

- Thêm registry bản quyền cục bộ trên ổ D: lưu key, tên máy, tên quán, ngày kích hoạt và trạng thái.
- Thêm Telegram: `/listkey`, `/deactivate "KEY"` và `/list`; lệnh `/activate "KEY"` tiếp tục kích hoạt ngay.
- Hủy kích hoạt khóa trang xem lại ngay nhưng không dừng ghi camera.
- Đóng gói phát hành: `CCTV_1.3.0.exe`.
- Sao lưu trước khi sửa: `backup/1.3.0/1_1.3.0.py.backup`.

## 1.2.10 - 2026-08-05

- Khôi phục `1.py` và `index.html` từ backup bản 1.2.9; các thay đổi 2.0 đã được lưu riêng tại `backup/1.2.10/` trước khi phục hồi.
- Khi chưa kích hoạt, ứng dụng vẫn ghi camera và bot Telegram chờ vô hạn lệnh `/activate "MÃ_MÁY"` từ chat quản trị.
- Chặn các chức năng xem lại, phát/tải, cắt và ghép video cho đến khi kích hoạt; không thay đổi logic ghi camera.
- Kích hoạt thành công có hiệu lực ngay, không cần khởi động lại, và được lưu theo mã máy.
- Sao lưu: `backup/1.2.10/1_before_restore_2.0.1.py.backup` và `backup/1.2.10/index_before_restore_2.0.1.html.backup`.

## 2.0.1 - 2026-08-05

- Khi chưa kích hoạt, ứng dụng không còn tắt: camera vẫn ghi và bot Telegram chờ vô hạn lệnh `/activate "MÃ_MÁY"` từ chat quản trị.
- Lệnh kích hoạt đúng mã máy mở bản quyền ngay, không cần khởi động lại ứng dụng.
- Bỏ cơ chế kích hoạt tự động từ tin nhắn ghim; chỉ lệnh `/activate "MÃ_MÁY"` từ chat quản trị mới kích hoạt máy chưa có cache hợp lệ.
- Chặn trang xem lại, danh sách, phát/tải/ghép/cắt video và API liên quan cho đến khi kích hoạt; không thay đổi luồng ghi camera.
- Đóng gói phát hành: `CCTV_2.0.1.exe`.
- Sao lưu trước khi sửa: `backup/2.0.1/1_2.0.1.py.backup`.

## 2.0.0 - 2026-08-04

Bản 2.0 chuyển từ việc suy đoán trạng thái qua tên file sang chỉ mục và trạng thái vận hành thực tế. Chi tiết nằm trong `CHANGELOG_2.0.0.md`.

Tệp này là nhật ký thay đổi chung của dự án. Từ phiên bản 1.2.5, mọi lần sửa mã nguồn đều phải bổ sung một mục ở đây và sao lưu tệp gốc vào thư mục `backup/<phiên_bản>/` trước khi sửa.

## 1.2.9 - 2026-08-04

- Thêm nút ngày trước/ngày sau cạnh ngày ghép; định dạng hiển thị `Ngày: dd/mm/yyyy` được giữ nguyên.
- Ghép video tự chuyển ngày kết thúc sang ngày hôm sau khi giờ kết thúc nhỏ hơn hoặc bằng giờ bắt đầu, ví dụ `23:00 → 00:30`.
- Đóng gói phát hành: `CCTV_1.2.9.exe`.
- Sao lưu trước khi sửa: `backup/1.2.9/1_1.2.9.py.backup` và `backup/1.2.9/index_1.2.9.html.backup`.

## 1.2.8 - 2026-08-04

- Ẩn cửa sổ FFmpeg khi ghi, cắt và ghép video bằng `CREATE_NO_WINDOW`.
- Giữ nguyên hiển thị ngày ghép dạng `Ngày: dd/mm/yyyy`; nhấn vào vùng ngày để mở lịch chọn ngày khác.
- Ngày ghép mặc định vẫn lấy mốc một giờ trước và không phụ thuộc vào bộ lọc danh sách xem lại.
- Sao lưu trước khi sửa: `backup/1.2.8/1_1.2.8.py.backup` và `backup/1.2.8/index_1.2.8.html.backup`.

## 1.2.7 - 2026-08-04

- FFmpeg ghi mỗi đoạn vào tệp tạm `.part`; chỉ sau khi kết thúc thành công, app mới đổi tên tệp sang `.mp4` và ghi log `Ghi xong`.
- Danh sách xem lại liệt kê toàn bộ MP4 hoàn tất, không còn ẩn video mới nhất hoặc suy đoán theo thời gian trong tên tệp.
- Mặc định trang xem lại hiển thị mọi đoạn bắt đầu trong một giờ gần nhất, gồm cả các đoạn thuộc ngày hôm trước khi qua nửa đêm.
- Bộ lọc một giờ tính theo phần thời gian giao nhau, nên không bỏ sót đoạn bắt đầu ngay trước mốc một giờ.
- Đóng gói phát hành: `CCTV_1.2.7.exe`.
- Sao lưu trước khi sửa: `backup/1.2.7/1_1.2.7.py.backup` và `backup/1.2.7/index_1.2.7.html.backup`.

## 1.2.5 - 2026-08-03

- Tối ưu danh sách video xem lại: không còn gọi FFmpeg để quét/kiểm tra từng tệp MP4.
- API xem lại sắp xếp các đoạn theo thời gian sửa tệp và luôn ẩn đoạn mới nhất, vì đó là đoạn đang được FFmpeg ghi hoặc hoàn tất.
- Các đoạn cũ hiển thị ngay khi mở trang xem lại.
- Sao lưu trước khi sửa: `backup/1.2.5/1_1.2.5.py.backup`.

## 1.2.6 - 2026-08-03

- Sửa việc ẩn video đang ghi: thay vì chỉ bỏ một tệp theo thời gian sửa, API đọc khung thời gian trong tên tệp và ẩn mọi đoạn có thời gian đang diễn ra.
- Hỗ trợ cả định dạng tệp bản 1.2 (`cam1_..._to_...`) và định dạng phân đoạn cũ (`cam1_segment_...`).
- Không dùng FFmpeg để kiểm tra MP4, nên danh sách vẫn hiển thị tức thì.
- Đóng gói phát hành: `CCTV_1.2.6.exe`.
- Sao lưu trước khi sửa: `backup/1.2.6/1_1.2.6.py.backup`.

## Lịch sử trước đó

- 1.2.4: tối ưu kiểm tra MP4, xử lý phát lại và đóng gói EXE.
- 1.2.3: cải thiện chuyển sang video kế tiếp và chức năng phát lại.
- 1.2.2: sửa logic ghi/phân đoạn video và phát lại.
- 1.2.1: khôi phục logic ghi video của bản 1.2 ban đầu và bổ sung quy trình sao lưu.

Các tệp `CHANGELOG_1.2.x.md` cũ được giữ lại để tham khảo; các thay đổi mới chỉ ghi vào tệp này.
