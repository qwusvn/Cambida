# PROJECT SYNC PROTOCOL — cambida (Remote-First Direct)

Version: 3.0 (Remote-First Direct Execution)  
Status: **ACTIVE**  
Historical Protocol: Protocol Drive/Watchdog v2.2 đã chính thức **RETIRED**. Bản lưu trữ lịch sử được bảo tồn tại `D:\Yato\legacy\cambida-drive-watchdog-20260909`. Các agent tuyệt đối **KHÔNG** được khởi động lại watcher cũ hoặc task queue qua Google Drive trừ khi có yêu cầu rollback cụ thể từ người dùng.

---

## 1. Mục tiêu & Định vị Kiến trúc

Kiến trúc điều phối dự án chuyển đổi hoàn toàn sang mô hình **Remote-First Direct**, loại bỏ cơ chế đồng bộ trung gian qua Google Drive watchdog:

- **Bộ não / Điều phối / Review**: `ChatGPT`
- **Control Plane**: `REMOTE_DESKTOP_COMMANDER` trực tiếp kết nối tới máy Yato (`D:\1\cambida`).
- **Primary Executor**: `AGY CLI` (bắt buộc chỉ định model `gemini-3.8-flash-high`, `--mode accept-edits` khi sửa code hoặc `--mode plan` khi khảo sát).
- **Secondary / Fallback Executor**: `Codex CLI` (dùng dự phòng hoặc đối chiếu khi cần).
- **Source of Truth**: `LOCAL_YATO_WORKTREE` (Local Git worktree trên máy Yato).
- **Google Drive**: Vai trò `LARGE_FILES_ONLY` — chỉ dùng khi cần đọc/truyền file dung lượng lớn, binary, video hoặc artifact cần cloud inspection. **Không còn là source-of-truth, không làm queue, không làm sync barrier, không dùng ACK hay kênh điều phối**.

---

## 2. Các thành phần đã RETIRED (Bãi bỏ)

Tất cả các cơ chế sau của protocol v2.2 đều bãi bỏ và không được kích hoạt:
1. **Watchdog process**: Không chạy watcher nền giám sát file/heartbeat.
2. **Cloud Manifest & ACKs**: Không dùng `MANIFEST.json`, `CLOUD_ACK.json`, `PC_ACK.json`, `READY_CANDIDATE.json`.
3. **Round-trip Drive Barrier**: Không còn sync barrier 3 nút (`PC ↔ Drive ↔ ChatGPT`).
4. **Task Queue qua Drive**: Không tạo hay tiêu thụ task queue qua Google Drive hoặc `.ai_flow/`.

---

## 3. Quy trình Điều phối & Thực thi Chuẩn

```
ChatGPT (Reviewer & Brain)
       │  (Điều khiển trực tiếp qua REMOTE_DESKTOP_COMMANDER)
       ▼
 Máy Yato (Local Worktree: D:\1\cambida)
       │
  ┌────┴────────────────────────┐
  ▼                             ▼
AGY CLI (gemini-3.8-flash-high)    Codex CLI (Fallback)
  │                             │
  └────► Sửa code / Chạy lệnh / Build / Test ◄────┘
       │
       ▼
ChatGPT tự đọc diff, test log & review trực tiếp trên Yato
```

1. **Khảo sát & Lập kế hoạch**:
   - ChatGPT đọc trạng thái trực tiếp trên máy Yato qua `REMOTE_DESKTOP_COMMANDER`.
   - ChatGPT lập kế hoạch và chia tách task rõ ràng.
2. **Giao việc Executor**:
   - Task code dài, sửa logic, build/test được giao cho `AGY CLI` với model `gemini-3.8-flash-high`, chế độ sửa code là `--mode accept-edits`, chế độ khảo sát là `--mode plan`.
   - `Codex CLI` làm executor phụ/fallback (gợi ý lệnh non-interactive đáng tin cậy qua stdin: `$prompt | codex exec -C 'D:\1\cambida' --sandbox workspace-write -`).
3. **Thực thi & Tự động Xác minh**:
   - Executor sửa source, chạy lệnh, build và chạy tests trực tiếp trong worktree Yato.
   - ChatGPT tự đọc diff, test output, terminal log qua `REMOTE_DESKTOP_COMMANDER` để thực hiện review.
   - **Quy tắc kiểm chứng**: Build, lint hoặc unit test không tự động là bằng chứng E2E hay thành công trên thiết bị thật.
4. **Vòng lặp Sửa đổi Tự động**:
   - Nếu kết quả chưa PASS hoặc chưa đạt yêu cầu, ChatGPT lập tức giao vòng sửa tiếp theo cho executor mà không cần người dùng phải copy/paste prompt thủ công.
5. **Chính sách Google Drive & Khi Yato Offline**:
   - Google Drive chỉ dùng khi cần truyền tải file lớn/binary/artifact lên cloud để ChatGPT xem xét; không dùng làm source-of-truth, task queue, sync barrier, heartbeat, manifest hay ACK.
   - Khi máy Yato offline, chỉ các dữ liệu đã upload lên Drive trước đó là đọc được. Tuyệt đối **không được suy luận** rằng dữ liệu trên Drive là source code mới nhất. Source of truth luôn là local worktree trên máy Yato.

---

## 4. Tương thích Ngược (Compatibility)

- File `SYNC_STATE.json` tại root được định dạng thành **compatibility tombstone** (non-authoritative) để các agent hoặc công cụ cũ nhận diện ngay trạng thái `REMOTE_DIRECT` và không kích hoạt watcher.
- Toàn bộ dữ liệu `.ai_flow/` và protocol v2.2 cũ được di dời nguyên trạng ra ngoài active project tại `D:\Yato\legacy\cambida-drive-watchdog-20260909`.
