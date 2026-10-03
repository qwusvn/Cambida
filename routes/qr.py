"""Table QR code generation routes and helpers for Cambida CCTV.

Generates QR codes strictly pointing to LAN IP so guests scan locally,
with iOS auto-redirecting to HTTPS at replay route.
"""

from io import BytesIO
import ipaddress
import logging
from flask import Blueprint, Response, request
from services.network import get_local_lan_ip

try:
    import qrcode
except ImportError:
    qrcode = None

logger = logging.getLogger(__name__)

qr_bp = Blueprint("qr", __name__)


def generate_table_qr(table_id, tables, server_port, req_host, url_root):
    """Generate PNG bytes of the QR code for a given table."""
    if qrcode is None:
        return None, "Thiếu thư viện qrcode. Hãy cài dependencies trước khi build.", 503

    table = next(
        (item for item in tables if str(item.get("id")) == str(table_id)),
        None,
    )
    if not table or not table.get("camera_id"):
        return None, "Bàn không tồn tại hoặc chưa gán camera", 404

    port = int(server_port or 8000)
    req_host_clean = req_host.split(":")[0].strip().lower()
    is_lan = False
    try:
        ip_obj = ipaddress.ip_address(req_host_clean)
        if ip_obj.is_private and not ip_obj.is_loopback:
            is_lan = True
    except ValueError:
        pass

    if is_lan:
        lan_base = url_root.rstrip("/")
    else:
        lan_ip = get_local_lan_ip()
        lan_base = f"http://{lan_ip}:{port}"

    target_url = f"{lan_base}/replay/cam{int(table['camera_id'])}"
    image = qrcode.make(target_url, border=2)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), None, 200
