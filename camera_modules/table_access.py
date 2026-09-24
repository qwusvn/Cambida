"""Flask integration for stable table IDs and server-side guest media access.

This module never changes the camera recorder or any recording worker.
The application supplies its existing config loader, saver and NVR reference lookup.
"""
from __future__ import annotations

import hmac
import re
import threading
from urllib.parse import urlsplit

from flask import jsonify, request, session


_TABLE_LOCK = threading.RLock()
_CAM_FILE = re.compile(r"^(?:merge_|cut_|nvr_(?:hik_|dahua_)?)?cam([1-9][0-9]*)_", re.I)
_CAM_ROUTE = re.compile(r"^/(?:replay/)?cam([1-9][0-9]*)$")
_LIST_ROUTE = re.compile(r"^/(?:list|snapshot)/cam([1-9][0-9]*)$")


def install_table_access(app, *, get_config, set_config, save_config,
                         get_nvr_reference, license_grant=None):
    """Register admin endpoints and deny all unprivileged access to switched-off media."""

    def tables():
        config = get_config()
        return config.get("tables", []) if isinstance(config.get("tables"), list) else []

    def effective_tables(config):
        existing = config.get("tables")
        if isinstance(existing, list) and existing:
            return existing
        return [
            {"id": f"ban-{index:02d}", "camera_id": index,
             "name": str(cam.get("name") or f"Bàn {index}"),
             "enabled": cam.get("enabled", True),
             "privacy_off": cam.get("privacy_off", False)}
            for index, cam in enumerate(config.get("cameras", []), start=1)
            if isinstance(cam, dict)
        ]

    def is_on(table):
        return table.get("enabled", True) is not False and not bool(table.get("privacy_off", False))

    def binding(cam_id):
        for table in tables():
            if isinstance(table, dict):
                try:
                    if int(table.get("camera_id")) == cam_id:
                        return table
                except (TypeError, ValueError):
                    pass
        return None

    def blocked_cam(cam_id):
        if not tables():  # Legacy installations without explicit tables remain accessible.
            return False
        table = binding(cam_id)
        # A camera without an explicit table binding keeps legacy guest access.
        # Only an existing, explicitly disabled table may turn media off.
        return table is not None and not is_on(table)

    def any_off():
        return any(isinstance(table, dict) and not is_on(table) for table in tables())

    def origin_ok():
        origin = request.headers.get("Origin")
        if not origin:
            return True
        try:
            return urlsplit(origin).netloc.lower() == request.host.lower()
        except ValueError:
            return False

    def json_error(reason, status=400):
        return jsonify({"ok": False, "error": reason}), status

    def admin_guard():
        if session.get("admin_authenticated") is not True:
            return json_error("Yêu cầu đăng nhập quản trị.", 401)
        if request.method not in ("GET", "HEAD") and not origin_ok():
            return json_error("Nguồn yêu cầu không hợp lệ.", 403)
        return None

    def video_cam_id(filename):
        name = str(filename or "").split("/")[-1]
        match = _CAM_FILE.match(name)
        return int(match.group(1)) if match else None

    def filename_blocked(filename):
        cam_id = video_cam_id(filename)
        if cam_id is not None:
            return blocked_cam(cam_id)
        # Cut output names have no embedded source camera ID. Deny ambiguous media
        # to all guests when any table is off, rather than allow a direct URL bypass.
        return any_off()

    @app.before_request
    def enforce_table_privacy():
        if session.get("admin_authenticated") is True:
            return None
        path = request.path
        match = _CAM_ROUTE.match(path) or _LIST_ROUTE.match(path)
        if match:
            if blocked_cam(int(match.group(1))):
                return json_error("Bàn hiện không cho phép khách xem video.", 403)
            return None
        if path in ("/live", "/timeline", "/api/timeline"):
            # Shared feeds/timelines contain several cameras; do not leak disabled tables.
            if any_off():
                return json_error("Luồng tổng hợp tạm ngừng khi có bàn đã tắt.", 403)
            return None
        if path == "/merge":
            try:
                cam_id = int(request.args.get("cam_id", ""))
            except ValueError:
                return json_error("Thiếu thông tin camera.", 400)
            if blocked_cam(cam_id):
                return json_error("Bàn hiện không cho phép khách cắt video.", 403)
        if path in ("/cut", "/cut_progress"):
            if filename_blocked(request.args.get("filename")):
                return json_error("Bàn hiện không cho phép khách cắt video.", 403)
        if path.startswith(("/video/", "/download/")):
            name = path.split("/", 2)[-1]
            if filename_blocked(name):
                return json_error("Bàn hiện không cho phép khách tải video.", 403)
        if path.startswith(("/nvr/video/", "/nvr/download/")):
            token = path.rsplit("/", 1)[-1]
            ref = get_nvr_reference(token)
            if not isinstance(ref, dict):
                return json_error("Đường dẫn video không hợp lệ.", 404)
            try:
                cam_id = int(ref.get("cam_id"))
            except (TypeError, ValueError):
                return json_error("Đường dẫn video không hợp lệ.", 404)
            if blocked_cam(cam_id):
                return json_error("Bàn hiện không cho phép khách tải video.", 403)
        return None

    @app.route("/api/admin/session", methods=["POST"])
    def table_admin_login():
        if not origin_ok():
            return json_error("Nguồn yêu cầu không hợp lệ.", 403)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return json_error("Yêu cầu JSON không hợp lệ.")
        auth = get_config().get("admin_auth") or {}
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        expected_username = str(auth.get("username", "admin"))
        expected_password = str(auth.get("password") or "")
        if (not expected_password or
                not hmac.compare_digest(username, expected_username) or
                not hmac.compare_digest(password, expected_password)):
            return json_error("Thông tin đăng nhập không hợp lệ.", 401)
        session.clear()
        session["admin_authenticated"] = True
        return jsonify({"ok": True})

    @app.route("/api/admin/tables", methods=["GET"])
    def table_admin_list():
        error = admin_guard()
        if error is not None:
            return error
        existing = effective_tables(get_config())
        return jsonify({"ok": True, "tables": [
            {"id": str(t.get("id")), "camera_id": t.get("camera_id"),
             "name": str(t.get("name", "")), "enabled": is_on(t),
             "privacy_off": not is_on(t),
             "sort_order": int(t.get("sort_order", index)),
             "color": "red" if is_on(t) else "grey"}
            for index, t in enumerate(existing, start=1)
            if isinstance(t, dict) and t.get("id") is not None
        ]})

    @app.route("/api/admin/tables/<path:table_id>/privacy", methods=["POST"])
    def table_admin_privacy(table_id):
        error = admin_guard()
        if error is not None:
            return error
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
            return json_error("Trường enabled phải là giá trị boolean.")
        with _TABLE_LOCK:
            config = get_config()
            existing = config.get("tables", [])
            if not isinstance(existing, list):
                return json_error("Cấu hình bàn không hợp lệ.", 409)
            existing = effective_tables(config)
            matches = [i for i, t in enumerate(existing) if isinstance(t, dict) and str(t.get("id")) == table_id]
            if len(matches) != 1:
                return json_error("Không tìm thấy bàn hoặc mã bàn không duy nhất.", 404)
            updated = dict(config)
            updated["tables"] = [dict(t) for t in existing]
            target = updated["tables"][matches[0]]
            target["enabled"] = payload["enabled"]
            target["privacy_off"] = not payload["enabled"]
            try:
                save_config(updated)
            except OSError:
                return json_error("Không thể lưu cấu hình bàn.", 500)
            set_config(updated)
            return jsonify({"ok": True, "id": table_id, "enabled": is_on(target)})

    @app.route("/api/admin/tables/reorder", methods=["POST"])
    def table_admin_reorder():
        error = admin_guard()
        if error is not None:
            return error
        payload = request.get_json(silent=True)
        ids = payload.get("ids") if isinstance(payload, dict) else None
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            return json_error("Cần danh sách mã bàn hợp lệ.")
        with _TABLE_LOCK:
            current = effective_tables(get_config())
            by_id = {str(t.get("id")): t for t in current if isinstance(t, dict) and t.get("id") is not None}
            if len(by_id) != len(current) or len(ids) != len(current) or set(ids) != set(by_id) or len(set(ids)) != len(ids):
                return json_error("Danh sách phải chứa chính xác toàn bộ mã bàn.", 400)
            updated = dict(get_config())
            updated["tables"] = [dict(by_id[table_id], sort_order=i) for i, table_id in enumerate(ids)]
            try:
                save_config(updated)
            except OSError:
                return json_error("Không thể lưu thứ tự bàn.", 500)
            set_config(updated)
            return jsonify({"ok": True, "ids": ids})

    @app.route("/api/admin/camera/probe", methods=["POST"])
    def table_camera_probe():
        error = admin_guard()
        if error is not None:
            return error
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return json_error("Thiếu thông tin thiết bị.")
        vendor = str(payload.get("vendor") or "").strip().lower()
        host = str(payload.get("host") or "").strip()
        user = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        defaults = {"hikvision": 8000, "ezviz": 8000, "dahua": 37777,
                    "imou": 37777, "kbvision": 8888, "rtsp": 554, "onvif": 80}
        if vendor not in defaults:
            return json_error("Giao thức không được hỗ trợ.")
        if not host or len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
            return json_error("Địa chỉ IP hoặc host không hợp lệ.")
        try:
            port = int(payload.get("port") or defaults[vendor])
        except (TypeError, ValueError, OverflowError):
            return json_error("Cổng kết nối không hợp lệ.")
        if not 1 <= port <= 65535 or not user or not password:
            return json_error("Cần cổng 1-65535, tài khoản và mật khẩu thiết bị.")
        if vendor in ("rtsp", "kbvision"):
            return json_error("Chưa hỗ trợ xác thực và liệt kê kênh SDK cho giao thức này; không tự giả định có một kênh.", 501)
        try:
            if vendor in ("hikvision", "ezviz"):
                from camera_modules.hikvision import probe_hikvision_device
                result = probe_hikvision_device(host, username=user, password=password,
                                                port=port, vendor=vendor)
                return jsonify({"ok": result.ok, "vendor": vendor,
                                "device_kind": result.device_kind, "transport": result.transport,
                                "channels": [{"channel_id": ch.channel_id, "name": ch.name,
                                              "channel_number": ch.channel_number,
                                              "online": ch.is_online} for ch in result.channels],
                                "message": str(result.message).replace(password, "***")}), (200 if result.ok else 503)
            if vendor in ("dahua", "imou"):
                from camera_modules.dahua import probe_dahua_device
                result = probe_dahua_device(host, username=user, password=password,
                                            port=port, timeout_sec=5.0)
                return jsonify({"ok": result.ok, "vendor": vendor,
                                "device_kind": "unknown", "transport": "netsdk" if result.ok else "none",
                                "channels": [{"channel_id": ch.channel_id, "name": ch.name}
                                             for ch in result.discovered_channels],
                                "message": str(result.message).replace(password, "***")}), (200 if result.ok else 503)
            from camera_modules.onvif import ONVIFAdapter
            adapter = ONVIFAdapter(host=host, port=port, username=user,
                                   password=password, timeout=5.0)
            result = adapter.probe(require_media=False, timeout=5.0)
            channels = adapter.get_channels() if result.ok else []
            return jsonify({"ok": result.ok, "vendor": "onvif", "device_kind": "unknown",
                            "transport": "onvif" if result.ok else "none",
                            "channels": [{"channel_id": ch.channel_id, "name": ch.name} for ch in channels],
                            "message": str(result.message).replace(password, "***")}), (200 if result.ok else 503)
        except Exception:
            # Do not expose driver exceptions that may include credentials or URLs.
            return json_error("Không thể xác thực thiết bị bằng giao thức đã chọn.", 503)

    @app.route("/api/admin/camera/discover", methods=["GET"])
    def table_camera_discover():
        error = admin_guard()
        if error is not None:
            return error
        from camera_modules.discovery import discover_lan_cameras
        try:
            devices = discover_lan_cameras(timeout_sec=2.0)
            return jsonify({"ok": True, "devices": [
                {"device_id": d.device_id, "ip": d.ip, "port": d.port,
                 "vendor": d.vendor, "protocol": d.protocol, "name": d.name,
                 "channel_count_hint": d.channel_count} for d in devices[:64]]})
        except Exception:
            return json_error("Không thể dò tìm thiết bị trong mạng nội bộ.", 503)

    if license_grant is not None:
        @app.before_request
        def enforce_verified_table_cap():
            if request.path != "/api/admin/config" or request.method != "PUT":
                return None
            if session.get("admin_authenticated") is not True:
                return None  # Existing admin_required handler supplies 401.
            if not origin_ok():
                return json_error("Nguồn yêu cầu không hợp lệ.", 403)
            candidate = request.get_json(silent=True)
            if not isinstance(candidate, dict):
                return None  # Existing config validation will reject it.
            new_count = max(len(candidate.get("tables", [])) if isinstance(candidate.get("tables"), list) else 0,
                            len(candidate.get("cameras", [])) if isinstance(candidate.get("cameras"), list) else 0)
            old_count = max(len(tables()), len(get_config().get("cameras", [])))
            if new_count <= old_count:
                return None
            try:
                grant = license_grant()
            except Exception:
                return json_error("Chưa thể xác thực hạn mức bàn trên Telegram.", 503)
            if not grant.is_verified:
                return json_error("Giấy phép chưa được xác thực.", 403)
            if grant.table_limit is not None and new_count > grant.table_limit:
                return json_error("Số bàn vượt quá hạn mức được xác thực.", 403)
            return None
