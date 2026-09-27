"""Script publish GitHub Release v2.2.3 and upload 2.2.3.zip using stored credentials."""
import os
import sys
import subprocess
import requests
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "2.2.3"
TAG_NAME = f"v{VERSION}"
ZIP_PATH = ROOT / "release" / f"{VERSION}.zip"
REPO = "qwusvn/Cambida"

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

def get_github_token():
    p = subprocess.run(
        ["git", "credential", "fill"],
        input=f"url=https://github.com/{REPO}.git\n",
        text=True,
        capture_output=True
    )
    creds = dict(line.split("=", 1) for line in p.stdout.strip().split("\n") if "=" in line)
    token = creds.get("password")
    if not token:
        raise RuntimeError("Không tìm thấy GitHub token từ git credential helper!")
    return token

def main():
    print(f"[*] Đang lấy GitHub token...", flush=True)
    token = get_github_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": f"Cambida-Publisher/{VERSION}",
    }

    if not ZIP_PATH.is_file():
        raise RuntimeError(f"Không tìm thấy file zip: {ZIP_PATH}")
    file_size = ZIP_PATH.stat().st_size
    print(f"[*] Tìm thấy file phát hành: {ZIP_PATH.name} ({file_size:,} bytes)", flush=True)

    # 1. Check if release already exists
    print(f"[*] Kiểm tra GitHub Release cho tag {TAG_NAME}...", flush=True)
    r = requests.get(f"https://api.github.com/repos/{REPO}/releases/tags/{TAG_NAME}", headers=headers)
    release_data = None
    if r.status_code == 200:
        release_data = r.json()
        print(f"[+] Đã tồn tại Release ID {release_data['id']}", flush=True)
    elif r.status_code == 404:
        print(f"[*] Đang tạo mới GitHub Release {TAG_NAME}...", flush=True)
        payload = {
            "tag_name": TAG_NAME,
            "target_commitish": "main",
            "name": f"Camera Highlight v{VERSION} - Host Loader, Cloudflare Routing & Launcher",
            "body": (
                f"### Camera Highlight v{VERSION}\n\n"
                "- **Kiến trúc Host Loader (camhl.exe) & Bytecode backend (_internal/app.pyc)**: Tách biệt hoàn toàn nhị phân cố định và logic ứng dụng, nâng cấp siêu tốc chỉ 0.1s.\n"
                "- **Định tuyến thông minh theo thiết bị**: Android / PC truy cập trực tiếp DDNS/LAN tốc độ cao; iPhone (iOS Safari) tự động chuyển hướng Cloudflare HTTPS kích hoạt Web Share API lưu 1 chạm vào Ảnh.\n"
                "- **Trình khởi động Taskbar (Chay_CCTV.cmd)**: Cửa sổ console trực quan trên Taskbar, kiểm tra cổng 8004 và tự động mở trình duyệt mặc định khi sẵn sàng.\n"
                "- **Bộ 3 nhị phân độc lập**: `camhl.exe`, `updater.exe`, `setup_cloudflare.exe`.\n"
                "- **Zero Config Pollution**: Gói `2.2.3.zip` sạch 100%, bảo toàn tuyệt đối cơ sở dữ liệu (`analytics.db`), cấu hình (`config.json`), token và kho video khi cập nhật."
            ),
            "draft": False,
            "prerelease": False,
        }
        create_res = requests.post(
            f"https://api.github.com/repos/{REPO}/releases",
            headers=headers,
            json=payload,
        )
        if create_res.status_code not in (200, 201):
            raise RuntimeError(f"Lỗi tạo release: {create_res.status_code} - {create_res.text}")
        release_data = create_res.json()
        print(f"[+] Tạo Release thành công! ID: {release_data['id']}", flush=True)
    else:
        raise RuntimeError(f"Lỗi kiểm tra release: {r.status_code} - {r.text}")

    upload_url_template = release_data.get("upload_url", "")
    upload_url = upload_url_template.split("{")[0]

    # 2. Check if asset 2.2.3.zip already uploaded
    existing_assets = release_data.get("assets", [])
    target_asset_name = f"{VERSION}.zip"
    for asset in existing_assets:
        if asset.get("name") == target_asset_name:
            print(f"[*] Đã tồn tại asset {target_asset_name} (ID: {asset['id']}), đang xóa bản cũ để tải bản mới...", flush=True)
            del_res = requests.delete(f"https://api.github.com/repos/{REPO}/releases/assets/{asset['id']}", headers=headers)
            print(f"[+] Xóa asset cũ: HTTP {del_res.status_code}", flush=True)

    # 3. Upload asset
    print(f"[*] Đang tải lên {target_asset_name} ({file_size:,} bytes) lên GitHub Release...", flush=True)
    upload_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/zip",
        "User-Agent": f"Cambida-Publisher/{VERSION}",
    }
    with open(ZIP_PATH, "rb") as f:
        up_res = requests.post(
            f"{upload_url}?name={target_asset_name}",
            headers=upload_headers,
            data=f,
            timeout=300,
        )

    if up_res.status_code not in (200, 201):
        raise RuntimeError(f"Lỗi tải asset lên GitHub: {up_res.status_code} - {up_res.text}")

    asset_info = up_res.json()
    print(f"[+] TẢI LÊN THÀNH CÔNG! Asset URL: {asset_info.get('browser_download_url')}", flush=True)
    print(f"[+] TỔNG KẾT: GitHub Release {TAG_NAME} đã sẵn sàng cho Auto-Update!", flush=True)

if __name__ == "__main__":
    main()
