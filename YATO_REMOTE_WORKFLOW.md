# YATO REMOTE WORKFLOW — CAMBIDA

Tài liệu này ghi nhận tuyến truy cập máy Yato khi thực hiện tác vụ Cambida. Với tác vụ do ChatGPT điều phối, nguồn quy tắc hiệu lực là `D:\1\gptagycodex.md`; bản sao `D:\1\Cambida\gptagycodex.md` chỉ để tham khảo.

## Tuyến thực thi

- SOL quyết định phạm vi và điều phối; YATO là giao diện công cụ/thao tác cục bộ trong cùng một chủ thể, không là tác nhân điều hành độc lập.
- Remote Desktop Commander có thể được sử dụng khi người dùng chỉ định, như tác vụ đồng bộ tài liệu ngày 22/09/2026.
- MCP hoặc CLI có thể được lựa chọn theo yêu cầu đã xác nhận và tình trạng thực tế của tuyến. Không tự thay đổi một executor hoặc transport mà người dùng đã chỉ định.
- AGY/Codex chỉ nhận nhiệm vụ cụ thể khi được phân công; phiên người dùng chạy trực tiếp trong các ứng dụng đó không thuộc điều phối của ChatGPT.
- Trước thực thi, kiểm tra tuyến được chọn, Git/worktree và bộ nhớ `.project`, bảo toàn các thay đổi đang có và task đang chạy.

## Phạm vi và nghiệm thu

- Nguồn kỹ thuật là source/Git tại `D:\1\Cambida`; Google Drive chỉ là nơi trao đổi artifact khi được cho phép, không thay thế Git hoặc biến thành hàng đợi điều phối.
- Chỉ thử nghiệm, regression, build chỉ để kiểm thử, hoặc thao tác device/emulator khi người dùng yêu cầu và xác nhận.
- Đối chiếu đầu ra thực tế với phạm vi được duyệt; không stage/commit thay đổi ngoài phạm vi và không tự dừng tiến trình không liên quan.
- Không khôi phục Drive queue/manifest/sync barrier/ACK cũ, không tự chuyển sang dự án khác.

Các hướng dẫn cũ về AGY CLI mặc định, Codex CLI fallback, model/mode bắt buộc và tự động chạy test trong tài liệu này đã được thay thế bởi quy tắc hiệu lực hiện hành.
