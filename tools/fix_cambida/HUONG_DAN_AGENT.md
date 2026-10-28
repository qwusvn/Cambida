# Cambida FixTool — hướng dẫn cho agent

Sửa máy Cambida cài **tại gốc ổ** (vd `D:\`) bị kẹt auto-update ở 2.3.4: bridge cũ
`updater.ps1` chết trước khi gọi `updater.exe`, app đã tắt nên mất ghi hình.

Công cụ chạy `updater.exe` **trực tiếp**, bỏ qua bridge, và chỉ thay file chương trình.

## 1. Chạy 1 lệnh

Mở Command Prompt (nên là **Administrator**) tại thư mục chứa `Fix_Cambida.cmd`:

```bat
Fix_Cambida.cmd
```

- Tự dò thư mục cài từ tiến trình Cambida đang chạy; nếu app đã chết sẽ hỏi đường dẫn.
- Tự lấy bản phát hành **mới nhất** trên GitHub (`qwusvn/Cambida`).

Chỉ định rõ thư mục cài (khuyến nghị khi app đã chết):

```bat
Fix_Cambida.cmd "D:\"
Fix_Cambida.cmd "D:\Cambida"
```

Máy không ra Internet — dùng ZIP tải sẵn:

```bat
Fix_Cambida.cmd "D:\" -ZipPath "C:\Users\Public\2.3.7.zip"
```

Kiểm tra trước, không thay đổi gì (tải + verify + in kết quả):

```bat
Fix_Cambida.cmd "D:\" -DryRun
```

Các tuỳ chọn khác: `-Version 2.3.7` (ghim phiên bản), `-Repo owner/repo`,
`-Token <token>` (repo private), `-Force` (cài lại dù đang cùng phiên bản).

## 2. Tool làm gì

1. Xác định thư mục cài và đọc `RELEASE_VERSION.txt`.
2. Tải `<version>.zip` (hoặc dùng ZIP có sẵn) vào `%TEMP%\cambida-fix-<guid>`.
3. Giải nén an toàn (chặn path traversal) và kiểm tra **toàn bộ SHA-256** theo
   `release_manifest.json`; bắt buộc `schema 2`, `kind: full`, version khớp,
   manifest liệt kê `updater.exe` + entrypoint, và **không có file nào ngoài manifest**.
4. Gọi trực tiếp:

   ```bat
   updater.exe --target "<thư mục cài>" --version <bản mới> --old-pid <pid> --payload "<payload>"
   ```

   `updater.exe` tự dừng Cambida + tiến trình con, ghi đè file chương trình theo
   `update_exclude.txt`, rồi khởi động lại app.
5. Chờ `GET http://127.0.0.1:<server_port>/api/ping` trả **200** (tối đa 120s).
6. Nếu ping thất bại: bật lại `Cambida.exe` cũ và báo lỗi kèm log.
7. Xoá thư mục tạm sau khi xong.

## 3. Tuyệt đối không

- Không copy/giải nén đè file bằng tay; không chạy `updater.ps1` trên máy đã chết.
- Không sửa/xoá `config.json`, `analytics.db`, `cctv_videos`, `nvr_cache`, `logs`,
  `tunnel_token.txt`, `controlhub_*`, `device_id.key`.
- Không cài mới sang thư mục khác, không đổi tên/di chuyển thư mục cài.
- Không đổi port trong `config.json` để "chữa" ping.

## 4. Nghiệm thu

| Mục | Cách kiểm tra | Kỳ vọng |
|---|---|---|
| Phiên bản | `type "D:\RELEASE_VERSION.txt"` | `2.3.7` |
| Ứng dụng sống | `curl http://127.0.0.1:<port>/api/ping` | HTTP 200, JSON `{"status":"ok"}` |
| Log cập nhật | `D:\logs\updater.log` | `Update 2.3.7 complete: overwritten ...` |
| Log tool | `D:\logs\fix-tool.log` | `updater.exe exit=0`, `Complete: installed=2.3.7` |
| Dữ liệu giữ nguyên | `config.json`, video, DB còn nguyên | Cùng nội dung trước/sau |
| Ghi hình | mở UI, xem camera/timeline | Ghi tiếp bình thường |

`<port>` lấy từ `server_port` trong `config.json` (mặc định 8000; nhiều máy cũ là 8004).

## 5. Mã thoát

- `0` — thành công thật: `updater.exe` trả 0, file trên đĩa đúng phiên bản đích và
  `/api/ping` trả 200; hoặc máy đã đúng phiên bản và ping OK; hoặc `-DryRun` verify xong.
- `1` — lỗi: không tải/verify được, updater trả mã khác 0 (kể cả khi app vẫn trả ping),
  file trên đĩa không đúng phiên bản đích, hoặc ping vẫn fail sau khi bật lại app.
  Xem `logs\fix-tool.log`, `logs\updater.log`, rồi chạy lại `-DryRun` để kiểm tra gói.
- Tool chỉ xoá thư mục tạm do chính nó tạo trong `%TEMP%`; không xoá thư mục cài,
  không xoá dữ liệu người dùng.

## 6. Bối cảnh lỗi (để agent không chẩn đoán sai)

- Bản 2.3.4 tải release mới, copy `updater.ps1` vào `%TEMP%`, chạy PowerShell rồi
  `os._exit(0)`. Với target `D:\`, bridge cũ làm:
  `Split-Path -Parent 'D:\'` → `''` → `Join-Path '' ...` ném lỗi → bridge chết,
  app đã tắt → không có ai bật lại và không gọi `updater.exe`.
- Lỗi phụ: target `D:\` bị ghép chuỗi thành `"D:\"`; dấu `\"` làm hỏng đối số,
  `updater.exe` nhận sai `--target` và thoát mã 2.
- Máy cài trong thư mục con (`D:\Cambida`) không bị vì parent khác rỗng.
- Từ 2.3.5, client gọi `updater.exe` trực tiếp; bridge chỉ còn để máy 2.3.4 cũ
  nâng cấp được. 2.3.7 phát hành bridge đã vá nên máy 2.3.4 còn sống tự lên 2.3.7
  trong vòng 1 giờ.

## 7. Fallback thủ công (khi không dùng được tool)

```powershell
# 1) Tải 2.3.7.zip và giải nén ra %TEMP%\payload
# 2) Xác minh hash theo release_manifest.json (sha256 từng file)
# 3) Chạy đúng 1 lệnh (chú ý giữ nguyên dấu ngoặc kép):
& "C:\...\payload\updater.exe" --target "D:\" --version 2.3.7 --old-pid 0 --payload "C:\...\payload"
# 4) Chờ /api/ping = 200; nếu fail: & "D:\Cambida.exe"
```

Không truyền `--payload` trỏ vào chính thư mục cài; payload phải nằm ngoài thư mục cài.
