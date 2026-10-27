# PROJECT SYNC PROTOCOL — RETIRED

Status: **RETIRED**
Workspace: `D:\1\cambida`

Protocol điều phối cũ bằng sync state / manifest / Drive queue / barrier không còn được sử dụng cho dự án này.

Quy chuẩn hiện hành nằm tại:

- `AGENTS.md`
- `.project/PROJECT.md`
- `.project/DECISIONS.md`
- `.project/STATE.md`
- `.project/TASKS.md`
- `.project/HANDOFF.md`
- `.project/CHANGELOG.md`

Source code thực tế và trạng thái Git trong `D:\1\cambida` là source of truth.

Không agent nào được tự khởi động lại cơ chế manifest, task queue, Drive ACK, sync barrier hoặc lock logic của protocol cũ chỉ vì file này còn tồn tại.
