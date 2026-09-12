# Quy ước phiên bản phát hành (2026-09-11)

- Chuỗi phiên bản phát hành chính thức bắt đầu từ 2.0.0.
- Dòng 2.0.x tăng patch mặc định; bản 2.1.0 được phát hành theo yêu cầu trực tiếp ngày 2026-09-12.
- Artifact phát hành chỉ lưu tại D:\1\cambida\release\<version>\.
- Các thư mục build/dist theo phiên bản ở root chỉ là tạm và phải dọn sau khi đóng gói.

# CCTV Changelog

## 2.1.0 - 2026-09-12

- **Hướng tạm thời mới nhất:** bỏ playback/cắt trực tiếp từ đầu ghi NVR trong luồng chính, tập trung `RTSP -> ghi MP4 local -> replay/cut bằng timeline`. Cấu hình NVR và adapter vẫn giữ nguyên trong source để có thể quay lại sau; `config.json` không bị sửa.
- Bổ sung runtime `RTSP_LOCAL_TIMELINE_ONLY`: mọi trang replay, `/list`, `/api/timeline` và `/merge` dùng kho file local sinh từ RTSP, kể cả camera đang có `playback_source=nvr`; recorder vẫn dùng RTSP URL hiện có của từng camera/kênh.
- `/list/camX` trong mode RTSP local lọc theo `date/start/end` ngay ở backend để timeline không phải tải toàn bộ lịch sử; tham số `source=nvr` cũng bị ép về local trong giai đoạn này để tránh phụ thuộc đầu ghi.
- Timeline UI vẫn giữ kim giữa cố định, coverage xanh/gap trống, không thumbnail và không ô nhập giờ.
- Kiểm chứng RTSP-local: `/list/cam2?source=nvr&date=...` vẫn trả source=`local`; `/video/...` hỗ trợ HTTP Range `206`; cắt thật 10 giây từ MP4 local cho output H.264 1080p đúng `10.00s`; `/api/timeline` báo toàn bộ camera source=`local`. Source test **35/35 PASS**.
- Sửa timestamp overlay local bị cộng offset hai lần: local MP4 dùng thời điểm bắt đầu segment làm `baseAt`, còn NVR (nếu bật lại sau này) mới dùng mốc `at=` làm base. CDP xác nhận file `17:29:38–17:34:38` tại `currentTime≈178s` hiển thị đúng `17:32:36`; kim vẫn cố định, Cut→Hủy giữ coverage và không có `/nvr/` ref.

- Khôi phục giao diện xem lại theo HTML thật trích từ `CCTV_1.3.42.exe`, đồng thời giữ backend NVR per-camera và adapter Dahua/Hikvision hiện tại.
- Trang xem lại NVR chỉ dùng **một nguồn NVR đã cấu hình sẵn**; bỏ hoàn toàn lựa chọn/nhãn `Server 1 / Server 2` khỏi giao diện.
- Replay chuyển hẳn sang **timeline-only**: bỏ ô nhập giờ và bỏ chế độ chọn giờ cũ; kim thời gian cố định giữa màn hình, kéo thước timeline chạy bên dưới, vùng NVR có bản ghi tô xanh, khoảng trống để trống và không dùng thumbnail.
- Timeline dùng trực tiếp `StartTime/EndTime` NVR làm hệ thời gian; khi thả kéo ở vùng có bản ghi, playback/download gọi NVR với `at=YYYY-MM-DDTHH:MM:SS` đúng mốc dưới kim.
- Màn hình cắt cũng dùng timeline-only; hai mốc bắt đầu/kết thúc là mốc tuyệt đối trên toàn ngày, có thể kéo qua ranh giới nhiều segment NVR. Backend giữ cơ chế báo gap và chỉ ghép phần có bản ghi.
- Task timeline không sửa `config.json`; SHA-256 workspace được giữ nguyên `ee7b820bea42c423acb05f15cd548c33706a6e0c981409ad320fec1085d0c8c3` trong toàn bộ quá trình kiểm thử.
- Sửa regression khi Hủy màn hình cắt quay lại replay: timeline được render lại sau khi màn hình hiện ra, nên vùng xanh và nhãn ruler không còn mất/cắt; Computer Use xác nhận PASS.
- Sửa lỗi chọn giờ NVR: khi nhập một mốc như `22:09`, giao diện chọn đúng segment chứa mốc đó (ví dụ `22:00:00–22:29:01`) thay vì luôn nhảy tới segment mới nhất; playback và tải đoạn NVR truyền `at=YYYY-MM-DDTHH:MM:SS` để bắt đầu đúng từ thời điểm đã chọn.
- `/merge` nay nhận biết camera nguồn NVR và cắt trực tiếp dữ liệu NVR theo khoảng thời gian yêu cầu; Dahua dùng `loadfile.cgi` theo mốc start/end chính xác, chia chunk theo `playback_chunk_sec`, sau đó chuẩn hóa H.264/AAC và concat khi cần. Local merge cũ vẫn giữ nguyên.
- Kiểm tra đầu ghi thật Cam 2 ngày 12/09/2026: NVR clock lệch PC khoảng 6 giây; mốc `22:09:00` nằm đúng trong segment `22:00:00–22:29:01`; cắt thật `22:09:00→22:09:10` tạo MP4 1080p H.264 duration `10.00s`.
- Theo yêu cầu mới nhất, **gỡ hoàn toàn cơ chế bản quyền Telegram ghim**: không còn `MachineGuid`, không còn khóa replay/timeline/cut/download, không còn watcher license và không còn lệnh `/license`/`/activate`; Telegram vẫn giữ cho cảnh báo/trạng thái hệ thống khác.
- Giữ bản sửa cắt/ghép chính xác tới giây: chỉ lấy phần media giao với khoảng chọn, hỗ trợ nhiều segment, cảnh báo gap và re-encode part để tránh sai mốc do keyframe.
- Kiểm thử source hiện tại: `python -m py_compile 1.py` đạt, JavaScript render qua `node --check` và **34/34 unittest PASS**.
- Đóng gói lại `CCTV_2.1.0.exe` bằng PyInstaller 6.16.0 dạng onedir; metadata EXE vẫn là FileVersion/ProductVersion `2.1.0`.
- Smoke test từ chính EXE với config cách ly không có Telegram: `/`, `/admin`, `/replay/cam1` và `/timeline` đều trả HTTP 200.
- Artifact phát hành: `D:\1\cambida\release\2.1.0\`; SHA-256 EXE sau bản sửa thời gian/cắt NVR: `f88eb22570632843f32db103598cc014fcb2595dc82a9463f2536e68c3689bbc`.

## 2.0.1 - 2026-09-12

- Hoàn tất chuyển mô hình cấu hình camera sang **mỗi kênh chỉ chọn một nguồn chính: `Local` hoặc `NVR`**; `Hybrid` không còn là chế độ cấu hình mới.
- Cấu hình NVR được lưu trực tiếp trên từng camera/kênh: hãng đầu ghi, host, HTTP/RTSP port, tài khoản, mật khẩu, kênh NVR và luồng bản ghi.
- Với camera nguồn `NVR`, bổ sung tùy chọn `backup_local` để ghi dự phòng luồng RTSP từ đầu ghi xuống máy tính. Khi bật, trang xem lại cho phép chọn `Server 1 - NVR` hoặc `Server 2 - Local`; khi tắt, không tạo worker ghi Local cho kênh đó.
- Bổ sung migration tương thích cấu hình cũ dùng `playback_source.mode = nvr/hybrid` và `channel_map`; khi lưu lại, cấu hình được chuẩn hóa về per-camera và bỏ block `playback_source` toàn cục.
- Sửa lỗi migration credential: camera legacy chuyển sang NVR giờ lấy đúng user/password của đầu ghi dùng chung thay vì giữ nhầm mật khẩu RTSP camera Local. Lỗi này trước đó gây `401 Unauthorized` khi kiểm tra Dahua.
- `/api/admin/test-camera` chỉ còn kiểm tra đúng nguồn đang chọn (`Local` hoặc `NVR`) và với NVR sẽ kiểm tra cả kết nối đầu ghi lẫn truy vấn recording trên đúng kênh.
- Timeline và trang xem lại hỗ trợ segment NVR với `play_url`/`download_url`; camera NVR có backup Local có thể chuyển nguồn phát lại trực tiếp trên trang xem lại.
- Trạng thái Admin phân biệt `NVR-only` với `NVR + dự phòng đang ghi`, không còn báo sai kênh có backup là NVR-only.
- Kiểm thử tự động: `python -m py_compile 1.py` và **29/29 unit/integration tests PASS**; JavaScript trong `admin.html`, `index.html`, `timeline.html` đều qua `node --check`.
- Kiểm chứng đầu ghi thật: Dahua DHI-XVR5108HS kết nối thành công, nhận diện 8 kênh và truy vấn 2 giờ gần nhất trả 3 segment NVR trên kênh kiểm tra.
- Đóng gói `CCTV_2.0.1.exe` bằng PyInstaller onedir. Smoke test từ chính EXE với config cách ly đạt HTTP 200 cho `/`, `/timeline` và `/admin`; metadata EXE hiển thị FileVersion/ProductVersion `2.0.1`.
- Artifact phát hành đặt tại `D:\1\Cambida\release\2.0.1\`.

## 1.3.51 - 2026-09-11

- Hoàn thiện tính năng "Kiểm tra camera" per-camera end-to-end cho cả 3 chế độ: `Local`, `NVR` và `Hybrid`.
- Giao diện Admin (`admin.html`) hỗ trợ và lưu giữ đầy đủ chế độ `Hybrid`:
  - Cho phép chọn nguồn `Local`, `NVR` hoặc `Hybrid` trực tiếp trong từng thẻ camera.
  - Khi chọn `Hybrid`: hiển thị đồng thời cả thông tin RTSP camera cục bộ (preset, IP, tài khoản, mật khẩu, port, path nâng cao) và ô `Kênh NVR`.
  - Hỗ trợ nút `Hybrid · Cả hai nguồn` trong popup thêm kênh mới.
  - Giữ nguyên thông tin cả hai nguồn trong `cameraValues()` và `setForm()`, không xóa trường RTSP hay kênh NVR khi ở chế độ Hybrid.
  - Nút kiểm tra hiển thị đúng ngữ cảnh: `Kiểm tra Local`, `Kiểm tra NVR` hoặc `Kiểm tra Hybrid`.
  - Kết quả kiểm tra Hybrid hiển thị màu sắc và thông điệp tương ứng từng nguồn (thành công, cảnh báo nếu chỉ 1 nguồn đạt, hoặc thất bại).
- Backend `1.py`:
  - `validate_config()` hỗ trợ đầy đủ `local`, `nvr`, `hybrid` cho từng camera; yêu cầu cả thông tin RTSP và kênh NVR khi chọn Hybrid.
  - Endpoint `/api/admin/test-camera`:
    - `Local`: kiểm tra độc lập luồng RTSP camera.
    - `NVR`: kiểm tra kết nối đầu ghi dùng chung trong Cài đặt NVR và thực hiện truy vấn tìm bản ghi trên đúng kênh `nvr_channel` được chỉ định.
    - `Hybrid`: kiểm tra độc lập cả hai nguồn (Local RTSP và NVR search) và trả về kết quả có cấu trúc riêng biệt (`sources: {"local": ..., "nvr": ...}`).
- Đã chạy kiểm thử E2E thực tế trên đầu ghi Dahua thật:
  - Dùng trực tiếp cấu hình trong `config.json` (`bidatamchin.ddns.net:81`, model DHI-XVR5108HS).
  - Kết nối và tìm bản ghi thật trên kênh đã map (Kênh 1), phản hồi `HTTP 200` với thông báo `NVR: Kết nối thành công · Kênh 1`; không dùng mock.
- Bộ test tự động (20/20 tests PASS):
  - `tests/test_camera_test.py` (9 tests): kiểm tra xác thực admin, validation, chế độ Local, chế độ NVR, chế độ Hybrid độc lập, và test thật Dahua NVR.
  - `tests/test_timeline.py` (6 tests): kiểm tra timeline API, cửa sổ thời gian và phân đoạn video.
  - `tests/test_v2.py` (5 tests): kiểm tra parser tên file, config validation và video index.
  - Kiểm tra cú pháp: `python -m py_compile 1.py` và `node --check` cho JavaScript giao diện quản trị đều đạt mã 0.
- Đóng gói bản phát hành dạng thư mục vào `release_2_0_0_folder` với EXE `CCTV_2.0.0.exe` đại diện cho `1.py` cùng các tệp đồng hành (`ffmpeg.exe`, `config.json`, các file HTML) đặt trực tiếp bên cạnh EXE.

## 1.3.50 - 2026-09-11

- Nút kiểm tra trong từng Camera/Bàn định tuyến theo đúng nguồn xem lại: `Local` kiểm tra RTSP camera; `NVR` dùng cấu hình trong thẻ Cài đặt NVR và đúng Kênh NVR; `Hybrid` kiểm tra cả hai nguồn và báo kết quả riêng.
- Khi chọn `NVR`, các trường IP/tài khoản/mật khẩu/Preset RTSP và phần RTSP nâng cao của camera Local được ẩn khỏi thẻ để tránh nhầm đây là thông tin dùng cho playback NVR; kênh NVR vẫn hiển thị rõ.
- API `/api/admin/test-camera` nhận kèm cấu hình NVR hiện tại từ biểu mẫu, kiểm tra xác thực đầu ghi rồi thực hiện truy vấn recording trên kênh được chọn trước khi báo thành công.
- Kiểm thử trực tiếp Dahua thật qua HTTP Digest + `mediaFileFind.cgi` đạt `200 OK`; kiểm thử Flask endpoint chế độ NVR trả `200` với `NVR: Kết nối thành công · Kênh 1`. Hybrid báo riêng Local timeout và NVR thành công, đúng logic tách nguồn.
- `python -m py_compile 1.py` và `node --check` cho JavaScript Admin đã đạt. Build PyInstaller onedir mới thành công tại `dist_2_0_0_source_logic/CCTV_2.0.0/`; chưa chạy E2E trực tiếp EXE mới để tránh can thiệp tiến trình CCTV đang vận hành.

## 1.3.49 - 2026-09-11

- Chuyển bản phát hành từ PyInstaller one-file sang onedir để EXE chỉ còn là entrypoint nhỏ, không phải tự giải nén toàn bộ runtime mỗi lần mở.
- Bật `noarchive=True` để module Python được tách ra `_internal` thay vì dồn vào EXE; EXE giảm còn khoảng 0,42 MB.
- `ffmpeg.exe`, `config.json` và các file giao diện HTML được phát hành trực tiếp cạnh `CCTV_2.0.0.exe` để dễ thay thế/kiểm tra độc lập.
- `_internal` chỉ giữ Python runtime, DLL và dependency; không chứa bản trùng FFmpeg/HTML. Riêng `config.json` mặc định vẫn được giữ nội bộ làm seed để tự phục hồi nếu config cạnh EXE bị xóa.
- Runtime ưu tiên `ffmpeg.exe` và template ở thư mục cạnh EXE, sau đó mới fallback vào bundle để giữ tương thích.
- Bản one-file trước thay đổi được backup tại `backup/1.3.49/CCTV_2.0.0_onefile.exe`.

## 1.3.48 - 2026-09-10

- Tách rõ cấu hình nguồn xem lại khỏi cấu hình đầu ghi: mỗi Camera/Bàn có lựa chọn riêng `Local`, `NVR` hoặc `Hybrid`.
- Thẻ `Cài đặt NVR` chỉ chứa thông tin đầu ghi dùng chung: hãng, IP/host, HTTP/RTSP port, tài khoản, mật khẩu, stream, timezone và timeout.
- `Kênh NVR` nằm ngay trong từng Camera/Bàn và chỉ có hiệu lực khi nguồn xem lại là `NVR` hoặc `Hybrid`.
- Backend timeline định tuyến theo từng camera: Local chỉ đọc video trên máy; NVR chỉ đọc đầu ghi; Hybrid ưu tiên NVR và dùng Local dự phòng khi NVR không có dữ liệu.
- Bỏ lựa chọn nguồn xem lại toàn cục khỏi Admin; `playback_source.mode` chỉ còn được tự sinh phía sau để tương thích cấu hình cũ.
- Cấu hình cũ có `playback_source.mode` và `channel_map` vẫn được tự chuyển lên giao diện mới khi mở Admin.
- Validate bắt buộc có kênh NVR khi một camera được đặt nguồn `NVR` hoặc `Hybrid`.
- Backup trước thay đổi tại `backup/1.3.48/`.

## 1.3.47 - 2026-09-10

- Đưa map NVR vào trực tiếp từng thẻ Camera/Bàn trong Admin bằng ô `Kênh NVR`; không còn phải nhập chuỗi `1:1,2:2,...` thủ công.
- Để trống `Kênh NVR` để camera đó không lấy bản ghi từ NVR.
- Cấu hình cũ `playback_source.nvr.channel_map` được tự nạp vào ô `Kênh NVR`, giữ tương thích ngược.
- Khi lưu Admin, `channel_map` vẫn được tự sinh phía sau để backend Hikvision/Dahua hiện tại tiếp tục hoạt động không đổi.
- Camera mới mặc định map theo số thứ tự Camera → cùng số Channel NVR, có thể sửa hoặc xóa giá trị ngay trên thẻ Camera.
- Backup trước thay đổi tại `backup/1.3.47/`.

## 1.3.46 - 2026-09-10

- Chuyển bản phát hành tạm thời sang portable one-file EXE: nhúng FFmpeg, template web và cấu hình mặc định vào `CCTV_2.0.0.exe`.
- Lần chạy đầu trong thư mục chỉ có EXE sẽ tự tạo `config.json`, `analytics.db`, `cctv_videos`, `logs` và `nvr_cache`; không cần đặt `ffmpeg.exe` cạnh EXE.
- Cấu hình được tạo ra cạnh EXE để các thay đổi trong Admin tiếp tục được lưu/persist như trước.
- Kiểm thử empty-folder đạt: trước chạy chỉ có EXE; web UI khởi động thành công, các file/thư mục runtime tự sinh đầy đủ.
- E2E Dahua từ chính portable EXE đạt: timeline trả 5 segment, `warnings=[]`; playback dùng FFmpeg nhúng, trả fragmented MP4 qua `dahua-cgi-stream`, nhận >500 KB dữ liệu đầu khoảng 0,85 giây trong phép thử.
- Backup trước thay đổi portable tại `backup/1.3.46/`.

## 1.3.45 - 2026-09-10

- Bổ sung adapter NVR Dahua song song Hikvision; Dahua dùng HTTP Digest + `mediaFileFind.cgi` để đọc metadata recording từ đầu ghi.
- Xác minh trực tiếp với DHI-XVR5108HS tại đầu ghi thật: đăng nhập thành công, truy vấn recording trả `StartTime`, `EndTime`, `FilePath`, `Length`, `CutLength`, `VideoStream`; firmware hiện expose 5 kênh video thực qua API.
- Playback Dahua tối ưu theo thời điểm được bấm: `loadfile.cgi?action=startLoad` chỉ lấy cửa sổ cần phát (mặc định 300 giây), FFmpeg chuyển DHAV sang fragmented MP4 và stream thẳng về trình duyệt; RTSP `/cam/playback` theo start/end giữ làm fallback.
- Download Dahua cũng giới hạn theo cửa sổ thời gian thay vì tải nguyên file DAV dài; kiểm thử 30 giây tạo MP4 khoảng 8,1 MB, H.264 1080p.
- Hybrid đổi thành NVR ưu tiên theo từng camera; chỉ dùng Local cho camera không map hoặc khi NVR camera đó không có dữ liệu/không dùng được, tránh clip Local/NVR chồng nhau.
- Admin hỗ trợ chọn `Hikvision ISAPI` hoặc `Dahua CGI + RTSP`, thêm RTSP port và thời lượng mỗi cửa sổ playback.
- Cấu hình triển khai hiện dùng Dahua qua `bidatamchin.ddns.net:81`, RTSP 554, map NVR 1-5 và giữ chế độ Hybrid để các camera còn lại tiếp tục dùng Local.
- E2E thật: timeline Dahua trả 5 camera không cảnh báo; endpoint video trả fragmented MP4 qua `dahua-cgi-stream`, nhận dữ liệu đầu tiên khoảng 1,1 giây trong phép thử; Python/JavaScript đều qua kiểm tra cú pháp.
- Sao lưu trước khi sửa tại `backup/1.3.45/` gồm `1.py`, `admin.html`, `timeline.html`, `config.json` và `CHANGELOG.md` bản trước.

## 1.3.44 - 2026-09-10

- Bổ sung nguồn xem lại `Local / NVR / Hybrid`; chế độ NVR dùng Hikvision ISAPI để tìm bản ghi trên đầu ghi thay vì phụ thuộc danh sách file cục bộ.
- Timeline gọi `POST /ISAPI/ContentMgmt/search`, ánh xạ camera sang track NVR, nhận `playbackURI` và chỉ tải/remux MP4 khi người dùng chọn đoạn cần xem hoặc tải.
- Bổ sung `/nvr/video/<token>`, `/nvr/download/<token>` và kiểm tra kết nối NVR qua `/api/admin/test-nvr`; liên kết NVR dùng token ngẫu nhiên có thời hạn và cache media tạm thời.
- Trang quản trị có cấu hình host/port/tài khoản/kênh/múi giờ NVR; mặc định vẫn là `Local` để không thay đổi hành vi hệ thống hiện tại.
- Khi bật `NVR` hoặc `Hybrid`, trang xem lại theo bàn chuyển sang timeline và tự chọn đúng camera; luồng ghi RTSP cục bộ hiện tại không bị thay đổi.
- Kiểm tra đã chạy: `python -m py_compile 1.py`, kiểm tra cú pháp JavaScript bằng Node, test mô phỏng ISAPI search/timezone/channel mapping và Flask routing/timeline local/NVR error path.
- Đóng gói thử nghiệm bằng `CCTV_2.0.0.spec`; bộ chạy nằm tại `release_2_0_0_nvr_api/` gồm EXE, `ffmpeg.exe` và `config.json`.
- Sao lưu trước khi sửa tại `backup/1.3.44/` gồm `1.py`, `admin.html`, `timeline.html`, `config.json` và `CHANGELOG.md` bản gốc.

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

## Legacy 2.0.1 - 2026-08-05 (retired numbering)

- Khi chưa kích hoạt, ứng dụng không còn tắt: camera vẫn ghi và bot Telegram chờ vô hạn lệnh `/activate "MÃ_MÁY"` từ chat quản trị.
- Lệnh kích hoạt đúng mã máy mở bản quyền ngay, không cần khởi động lại ứng dụng.
- Bỏ cơ chế kích hoạt tự động từ tin nhắn ghim; chỉ lệnh `/activate "MÃ_MÁY"` từ chat quản trị mới kích hoạt máy chưa có cache hợp lệ.
- Chặn trang xem lại, danh sách, phát/tải/ghép/cắt video và API liên quan cho đến khi kích hoạt; không thay đổi luồng ghi camera.
- Đóng gói phát hành: `CCTV_2.0.1.exe`.
- Sao lưu trước khi sửa: `backup/2.0.1/1_2.0.1.py.backup`.

## Legacy 2.0.0 - 2026-08-04 (retired numbering)

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
