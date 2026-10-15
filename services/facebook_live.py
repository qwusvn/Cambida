"""Opt-in guest Facebook Live encoder, isolated from the recording/replay pipeline.

Supports dynamic pool of Fanpage stream keys (idle slot allocation), custom ingest,
and authenticated personal guest profiles with encrypted stream keys.
A viewer must open the correct replay page to obtain a session-bound nonce.
PINs, stream keys, device tokens and target URLs are never exposed in API JSON.

NOTE ON CONCURRENCY & MEMORY RESERVATION:
Stream slot reservations, fingerprint tracking, and active session caps are managed
in-memory under an RLock. This design assumes a single-process deployment model
(one worker process per host). Duplicate destination fingerprint checks prevent
conflicting FFmpeg instances from colliding on the same Facebook stream key.
"""
from __future__ import annotations

import atexit
import hashlib
import hmac
import json
import re
import secrets
import subprocess
import threading
import time
from urllib.parse import urlsplit

from flask import jsonify, make_response, request, session

from services.facebook_profiles import ProfileError
from services.meta_page_live import MetaLiveError, valid_page_config

FACEBOOK_INGEST_HOSTS = frozenset({
    "live-api-s.facebook.com",
    "rtmp-api.facebook.com",
    "live-api.facebook.com",
})
DEFAULT_FACEBOOK_SERVER = "rtmps://live-api-s.facebook.com:443/rtmp/"
MAX_SESSIONS = 6
DEFAULT_MAX_SECONDS = 3600
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{8,256}$")
PROFILE_COOKIE_NAME = "__Secure-cambida_live_device"
PROFILE_COOKIE_MAX_AGE = 7776000  # 90 days in seconds
MAX_PAYLOAD_BYTES = 4096


def facebook_target(server_url: str, key: str) -> str:
    """Only Facebook RTMPS ingest is allowed (not a generic URL relay)."""
    if not isinstance(server_url, str) or not isinstance(key, str):
        raise ValueError("Thiếu thông tin Facebook Live.")
    key = key.strip()
    if not _KEY_RE.fullmatch(key):
        raise ValueError("Stream Key không hợp lệ.")
    try:
        parsed = urlsplit(server_url.strip())
        port = parsed.port
    except ValueError:
        raise ValueError("Địa chỉ máy chủ Facebook không hợp lệ.") from None
    if (parsed.scheme != "rtmps" or parsed.hostname not in FACEBOOK_INGEST_HOSTS
            or parsed.username or parsed.password or port not in (None, 443)
            or parsed.path.rstrip("/") not in ("", "/rtmp")
            or parsed.query or parsed.fragment):
        raise ValueError("Chỉ hỗ trợ máy chủ Facebook RTMPS chính thức.")
    return f"rtmps://{parsed.hostname}:443/rtmp/{key}"


def target_fingerprint(output_url: str) -> str:
    """Deterministic normalized fingerprint for conflict detection across stream modes."""
    return hashlib.sha256(output_url.strip().encode("utf-8")).hexdigest()


def ffmpeg_command(ffmpeg_path: str, rtsp_url: str, output: str) -> list[str]:
    """Use independent encoder; never change the existing capture/record workers."""
    return [
        ffmpeg_path, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-timeout", "10000000",
        "-i", rtsp_url,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "scale=w=1280:h=720:force_original_aspect_ratio=decrease,"
               "pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-b:v", "2000k", "-maxrate", "2200k", "-bufsize", "4400k",
        "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-c:a", "aac", "-ar", "44100", "-b:a", "128k",
        "-shortest", "-f", "flv", output,
    ]


class GuestFacebookLive:
    def __init__(self, enabled, allowed, get_source, ffmpeg_path,
                 get_page_config=None, popen=None, clock=None, monitor=True,
                 profile_store=None, meta_client=None):
        self.enabled = enabled
        self.allowed = allowed
        self.get_source = get_source
        self.ffmpeg_path = ffmpeg_path
        self.get_page_config = get_page_config
        self.popen = popen or subprocess.Popen
        self.clock = clock or time.monotonic
        self.monitor = monitor
        self.profile_store = profile_store
        self.meta_client = meta_client
        self.live_approvals = {}  # Staff-issued, camera-bound, one-use permission window.
        self.lock = threading.RLock()
        self.streams = {}
        if monitor:
            atexit.register(self.stop_all)

    @staticmethod
    def issue_nonce(cam_id):
        owner = session.get("_facebook_live_owner")
        if not owner:
            owner = secrets.token_urlsafe(24)
            session["_facebook_live_owner"] = owner
        nonces = dict(session.get("_facebook_live_nonces") or {})
        nonces[str(cam_id)] = secrets.token_urlsafe(32)
        session["_facebook_live_nonces"] = nonces
        return nonces[str(cam_id)]

    @staticmethod
    def _valid_nonce(cam_id, token):
        expected = (session.get("_facebook_live_nonces") or {}).get(str(cam_id))
        return (bool(expected) and isinstance(token, str)
                and hmac.compare_digest(expected, token)
                and bool(session.get("_facebook_live_owner")))

    @staticmethod
    def _origin_ok(effective_secure=None):
        origin = request.headers.get("Origin")
        if not origin:
            return request.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")
        from urllib.parse import urlsplit as parse
        try:
            target = parse(origin)
            expected_scheme = ("https" if effective_secure else "http") if effective_secure is not None else request.scheme
            return (target.scheme == expected_scheme
                    and target.netloc.lower() == request.host.lower()
                    and not target.path and not target.query and not target.fragment)
        except ValueError:
            return False

    def approve(self, cam_id, seconds=1200):
        with self.lock:
            if not self.allowed(cam_id):
                raise ValueError("Bàn không hoạt động hoặc đang riêng tư.")
            self.live_approvals[cam_id] = self.clock() + min(max(int(seconds), 60), 1800)
            return max(0, int(self.live_approvals[cam_id] - self.clock()))

    def revoke(self, cam_id):
        with self.lock:
            self.live_approvals.pop(cam_id, None)
            stream = self.streams.get(cam_id)
            if stream and stream.get("mode") == "page_api":
                self._end(cam_id, stream)

    def _approval_remaining(self, cam_id):
        return max(0, int(self.live_approvals.get(cam_id, 0) - self.clock()))

    def _meta_ready(self):
        if self.meta_client is None:
            return False
        try:
            return valid_page_config(self.get_page_config())
        except Exception:
            return False

    def _end(self, cam_id, stream):
        stream["shutdown_pending"] = True
        proc = stream.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if proc.poll() is None:
                raise RuntimeError("Không thể dừng bộ mã hóa livestream.")
        if stream.get("mode") == "page_api" and stream.get("live_video_id"):
            # The RTMPS disconnect alone must never be mistaken for ending a Page post.
            # Keep the camera reserved on Meta error and retry in subsequent monitor cycles.
            self.meta_client.end_live(stream["live_video_id"])
        self.streams.pop(cam_id, None)

    def _reap(self):
        now = self.clock()
        for cam_id, data in list(self.streams.items()):
            try:
                live = bool(self.allowed(cam_id))
                max_sec = data.get("max_seconds", DEFAULT_MAX_SECONDS)
                proc = data.get("proc")
                is_dead = (proc is None) or (proc.poll() is not None)
                should_stop = (data.get("shutdown_pending", False) or not self.enabled()
                               or not live or now - data["since"] >= max_sec or is_dead)
                if not should_stop and data.get("mode") == "personal":
                    should_stop = (self.profile_store is None or not self.profile_store.authorized(
                        data.get("profile_id"), data.get("authorization")))
            except Exception:
                # Unknown authorization or process state must fail closed.
                should_stop = True
            if should_stop:
                data["shutdown_pending"] = True
                try:
                    self._end(cam_id, data)
                except Exception:
                    # Retain the reservation and retry without blocking other streams.
                    pass

    def _watch(self, cam_id, proc):
        while True:
            time.sleep(2)
            try:
                with self.lock:
                    data = self.streams.get(cam_id)
                    if not data or data.get("proc") is not proc:
                        return
                    self._reap()
            except Exception:
                # Keep retrying shutdown without releasing a live process's reservation.
                continue

    def _get_slots(self):
        cfg = self.get_page_config() if callable(self.get_page_config) else {}
        slots = cfg.get("slots")
        if isinstance(slots, list) and slots:
            return [s for s in slots if isinstance(s, dict) and str(s.get("stream_key") or "").strip()]
        page_key = str(cfg.get("stream_key") or "").strip()
        if page_key:
            return [{
                "id": "default",
                "name": "Fanpage",
                "stream_key": page_key,
                "page_url": str(cfg.get("page_url") or "").strip(),
                "server_url": str(cfg.get("server_url") or DEFAULT_FACEBOOK_SERVER).strip(),
            }]
        return []

    def stop_profile_streams(self, profile_id):
        """Terminate all active streams belonging to a given guest profile ID."""
        if not profile_id:
            return
        with self.lock:
            failed = False
            for cam_id, data in list(self.streams.items()):
                if data.get("profile_id") == profile_id:
                    try:
                        self._end(cam_id, data)
                    except Exception:
                        failed = True
            if failed:
                raise RuntimeError("Không thể dừng tất cả phiên phát của tài khoản.")

    def start(self, cam_id, owner, server=None, key=None, mode=None, profile_id=None,
              authorization=None, expires=None):
        if not self.enabled():
            return {"error": "Quán chưa bật tính năng Facebook Live."}, 403
        if not self.allowed(cam_id):
            return {"error": "Bàn đang tắt hoặc ở chế độ riêng tư."}, 403

        cfg = self.get_page_config() if callable(self.get_page_config) else {}
        default_server = str(cfg.get("server_url") or DEFAULT_FACEBOOK_SERVER).strip()
        max_duration = min(max(int(cfg.get("max_duration_minutes") or 60), 5), 240) * 60

        with self.lock:
            self._reap()

            if cam_id in self.streams:
                return {"error": "Bàn này đang có phiên livestream."}, 409

            if len(self.streams) >= MAX_SESSIONS:
                return {"error": "Đã đạt giới hạn phiên livestream tại quán."}, 429

            active_fingerprints = {s["fingerprint"] for s in self.streams.values() if s.get("fingerprint")}

            if mode == "personal":
                if (not profile_id or not isinstance(authorization, str) or not authorization
                        or isinstance(expires, bool) or not isinstance(expires, (int, float))):
                    return {"error": "Phiên đăng nhập cá nhân không hợp lệ."}, 401
                if not isinstance(key, str):
                    return {"error": "Stream Key cho tài khoản cá nhân không hợp lệ."}, 400
                target_key = key.strip()
                if not target_key:
                    return {"error": "Không tìm thấy Stream Key cho tài khoản cá nhân."}, 400
                target_server = DEFAULT_FACEBOOK_SERVER
                page_url = ""
                slot_id = None
                slot_name = "Cá nhân"
                stream_mode = "personal"
            elif mode is None and str(key or "").strip():
                # Custom ingest mode (raw key passed directly)
                target_key = str(key or "").strip()
                target_server = str(server or default_server).strip()
                page_url = ""
                slot_id = None
                slot_name = "Tự cấu hình"
                stream_mode = "custom"
            elif mode is None and not str(key or "").strip() and self._meta_ready():
                if self._approval_remaining(cam_id) <= 0:
                    return {"error": "Nhân viên chưa cho phép phát Live bàn này. Vui lòng liên hệ quầy."}, 403
                target_key = None
                target_server = None
                page_url = ""
                slot_id = None
                slot_name = "Fanpage CLB"
                stream_mode = "page_api"
            elif mode is None and not str(key or "").strip():
                # Legacy persistent-key Fanpage mode (fallback only)
                slots = self._get_slots()
                if not slots:
                    return {"error": "Quán chưa cấu hình Stream Key cho Fanpage trên hệ thống."}, 422

                busy_slot_ids = {s.get("slot_id") for s in self.streams.values() if s.get("slot_id")}
                free_slot = None
                for slot in slots:
                    if slot.get("id") in busy_slot_ids:
                        continue
                    s_key = str(slot.get("stream_key") or "").strip()
                    s_server = str(slot.get("server_url") or default_server).strip()
                    try:
                        s_out = facebook_target(s_server, s_key)
                        s_fp = target_fingerprint(s_out)
                    except ValueError:
                        continue
                    if s_fp in active_fingerprints:
                        continue
                    free_slot = slot
                    break

                if not free_slot:
                    return {
                        "error": "Tất cả các luồng Fanpage của quán đều đang có bàn sử dụng. Vui lòng chờ bàn khác kết thúc."
                    }, 409

                target_key = str(free_slot["stream_key"]).strip()
                target_server = str(server or free_slot.get("server_url") or default_server).strip()
                page_url = str(free_slot.get("page_url") or "").strip()
                slot_id = free_slot.get("id")
                slot_name = free_slot.get("name") or "Fanpage"
                stream_mode = "fanpage"
            else:
                return {"error": "Chế độ phát sóng không hợp lệ."}, 400

            live_video_id = None
            if stream_mode == "page_api":
                try:
                    source = self.get_source(cam_id)
                except Exception:
                    source = None
                if not source or not str(source).startswith("rtsp://"):
                    return {"error": "Camera này chưa hỗ trợ phát Facebook qua RTSP."}, 422
                try:
                    created = self.meta_client.create_live(f"Trực tiếp bàn {cam_id} - Cambida")
                except MetaLiveError as exc:
                    return {"error": str(exc)}, 503
                live_video_id = created["id"]
                output = created["ingest"]
                page_url = created.get("permalink_url") or ""
            else:
                try:
                    output = facebook_target(target_server, target_key)
                except ValueError as exc:
                    return {"error": str(exc)}, 400

            fp = target_fingerprint(output)
            if fp in active_fingerprints:
                if live_video_id:
                    try:
                        self.meta_client.end_live(live_video_id)
                    except MetaLiveError:
                        pass
                return {"error": "Đường phát này đang được sử dụng ở bàn khác."}, 409

            try:
                source = self.get_source(cam_id)
            except Exception:
                source = None
            if not source or not str(source).startswith("rtsp://"):
                if live_video_id:
                    try:
                        self.meta_client.end_live(live_video_id)
                    except MetaLiveError:
                        pass
                return {"error": "Camera này chưa hỗ trợ phát Facebook qua RTSP."}, 422

            command = ffmpeg_command(self.ffmpeg_path, source, output)
            if stream_mode == "personal":
                if self.profile_store is None:
                    return {"error": "Dịch vụ tài khoản cá nhân hiện chưa sẵn sàng."}, 503
                try:
                    authorized = self.profile_store.authorized(profile_id, authorization)
                except Exception:
                    return {"error": "Dịch vụ tài khoản cá nhân hiện chưa sẵn sàng."}, 503
                if not authorized:
                    return {"error": "Phiên đăng nhập đã hết hạn hoặc bị thu hồi."}, 401
            try:
                proc = self.popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                if live_video_id:
                    try:
                        self.meta_client.end_live(live_video_id)
                    except MetaLiveError:
                        # Keep a pending reservation until Meta confirms the end.
                        self.streams[cam_id] = {
                            "proc": None, "owner": owner, "since": self.clock(),
                            "max_seconds": max_duration, "mode": "page_api",
                            "live_video_id": live_video_id, "shutdown_pending": True,
                            "fingerprint": fp,
                        }
                return {"error": "Không khởi động được bộ mã hóa livestream."}, 503

            if stream_mode == "page_api":
                self.live_approvals.pop(cam_id, None)

            self.streams[cam_id] = {
                "proc": proc,
                "owner": owner,
                "since": self.clock(),
                "max_seconds": max_duration,
                "mode": stream_mode,
                "live_video_id": live_video_id,
                "permalink_last_checked": 0,
                "page_mode": (stream_mode in ("fanpage", "page_api")),
                "slot_id": slot_id,
                "slot_name": slot_name,
                "page_url": page_url,
                "fingerprint": fp,
                "profile_id": profile_id,
                "authorization": authorization if stream_mode == "personal" else None,
                "expires": expires if stream_mode == "personal" else None,
                "shutdown_pending": False,
            }
            if self.monitor:
                threading.Thread(target=self._watch, args=(cam_id, proc),
                                 daemon=True, name=f"FacebookLiveCam{cam_id}").start()

        if stream_mode == "page_api":
            msg = "Đã tạo buổi Live trên Fanpage qua Meta API và bắt đầu gửi camera. Đang chờ tín hiệu phát."
        elif stream_mode == "fanpage":
            msg = f"Đã bắt đầu truyền hình ảnh bàn lên {slot_name}."
        else:
            msg = "Đã gửi luồng tới Facebook. Kiểm tra Live Producer và chọn Phát trực tiếp."

        return {
            "ok": True,
            "state": "connecting",
            "page_url": page_url,
            "slot_name": slot_name,
            "message": msg,
        }, 200

    def status(self, cam_id, owner):
        with self.lock:
            self._reap()
            slots = self._get_slots()
            page_api_ready = self._meta_ready()
            configured = bool(slots) or page_api_ready
            cam_stream = self.streams.get(cam_id)
            camera_busy = bool(cam_stream)
            total_active = len(self.streams)
            is_enabled = bool(self.enabled())

            active_fingerprints = {s["fingerprint"] for s in self.streams.values() if s.get("fingerprint")}
            busy_slot_ids = {s.get("slot_id") for s in self.streams.values() if s.get("slot_id")}

            cfg = self.get_page_config() if callable(self.get_page_config) else {}
            default_server = str(cfg.get("server_url") or DEFAULT_FACEBOOK_SERVER).strip()

            has_free_slot = False
            for slot in slots:
                if slot.get("id") in busy_slot_ids:
                    continue
                s_key = str(slot.get("stream_key") or "").strip()
                s_server = str(slot.get("server_url") or default_server).strip()
                try:
                    s_out = facebook_target(s_server, s_key)
                    s_fp = target_fingerprint(s_out)
                except ValueError:
                    continue
                if s_fp in active_fingerprints:
                    continue
                has_free_slot = True
                break

            approval_seconds = self._approval_remaining(cam_id) if page_api_ready else 0
            fanpage_available = is_enabled and (not camera_busy) and (total_active < MAX_SESSIONS) and (
                approval_seconds > 0 if page_api_ready else has_free_slot)
            personal_available = is_enabled and self.profile_store is not None and (not camera_busy) and (total_active < MAX_SESSIONS)

            if cam_stream:
                is_owner = (cam_stream["owner"] == owner)
                if (is_owner and cam_stream.get("mode") == "page_api"
                        and not cam_stream.get("page_url")
                        and not cam_stream.get("shutdown_pending")
                        and self.clock() - cam_stream.get("permalink_last_checked", 0) >= 15):
                    cam_stream["permalink_last_checked"] = self.clock()
                    try:
                        cam_stream["page_url"] = self.meta_client.permalink(cam_stream["live_video_id"])
                    except Exception:
                        pass
                stopping = bool(cam_stream.get("shutdown_pending"))
                result = {
                    "ok": True,
                    "enabled": is_enabled,
                    "configured": configured,
                    "page_api_enabled": page_api_ready,
                    "approval_seconds": approval_seconds,
                    "camera_busy": True,
                    "fanpage_available": False,
                    "personal_available": False,
                    "mode": cam_stream.get("mode"),
                    "state": ("stopping" if stopping else "running") if is_owner else "busy",
                    "is_owner": is_owner,
                    "page_url": cam_stream.get("page_url", ""),
                    "slot_name": cam_stream.get("slot_name", ""),
                    "seconds": int(self.clock() - cam_stream["since"]) if is_owner else None,
                }
                if stopping and is_owner:
                    result["message"] = "Đang dừng phiên phát. Có thể thử dừng lại."
                return result

            if not page_api_ready and slots and not has_free_slot:
                return {
                    "ok": True,
                    "enabled": is_enabled,
                    "configured": configured,
                    "page_api_enabled": page_api_ready,
                    "approval_seconds": approval_seconds,
                    "camera_busy": False,
                    "fanpage_available": False,
                    "personal_available": personal_available,
                    "mode": None,
                    "state": "busy",
                    "is_owner": False,
                    "busy_message": "Tất cả các luồng Fanpage của quán đều đang có bàn sử dụng.",
                    "page_url": "",
                }

            return {
                "ok": True,
                "enabled": is_enabled,
                "configured": configured,
                "page_api_enabled": page_api_ready,
                "approval_seconds": approval_seconds,
                "camera_busy": False,
                "fanpage_available": fanpage_available,
                "personal_available": personal_available,
                "mode": None,
                "state": "idle",
                "is_owner": False,
                "page_url": "",
            }

    def stop(self, cam_id, owner):
        with self.lock:
            self._reap()
            data = self.streams.get(cam_id)
            if not data:
                return {"ok": True, "state": "idle"}, 200
            if data["owner"] != owner:
                return {"error": "Phiên này thuộc thiết bị khác."}, 403
            self._end(cam_id, data)
            return {"ok": True, "state": "idle"}, 200

    def stop_all(self):
        with self.lock:
            failed = False
            for cam_id, data in list(self.streams.items()):
                try:
                    self._end(cam_id, data)
                except Exception:
                    failed = True
            if failed:
                raise RuntimeError("Không thể dừng tất cả phiên phát.")


def install_facebook_live(app, enabled, allowed, get_source, ffmpeg_path, secure,
                          get_page_config=None, popen=None, monitor=True,
                          profile_store=None, meta_client=None):
    """Register guest-facing endpoints; existing remote-auth middleware stays in force."""
    manager = GuestFacebookLive(enabled, allowed, get_source, ffmpeg_path,
                                get_page_config=get_page_config,
                                popen=popen, monitor=monitor,
                                profile_store=profile_store,
                                meta_client=meta_client)

    @app.after_request
    def facebook_live_no_store(resp):
        if request.path.startswith("/api/facebook-live/"):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
        return resp

    def _no_store_json(payload, status=200):
        resp = make_response(jsonify(payload), status)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp

    def _set_device_cookie(resp, token):
        resp.set_cookie(
            PROFILE_COOKIE_NAME,
            token,
            max_age=PROFILE_COOKIE_MAX_AGE,
            httponly=True,
            secure=True,
            samesite="Strict",
            path="/api/facebook-live/",
        )
        return resp

    def _clear_device_cookie(resp):
        resp.set_cookie(
            PROFILE_COOKIE_NAME,
            "",
            max_age=0,
            expires=0,
            httponly=True,
            secure=True,
            samesite="Strict",
            path="/api/facebook-live/",
        )
        return resp

    def _read_json_payload():
        if (request.content_length or 0) > MAX_PAYLOAD_BYTES:
            return None, ("Dung lượng yêu cầu vượt quá giới hạn cho phép.", 400)
        if not request.is_json:
            return None, ("Dữ liệu gửi lên không đúng định dạng JSON.", 400)
        try:
            raw = request.stream.read(MAX_PAYLOAD_BYTES + 1)
            if len(raw) > MAX_PAYLOAD_BYTES:
                return None, ("Dung lượng yêu cầu vượt quá giới hạn cho phép.", 400)
            data = json.loads(raw)
        except Exception:
            return None, ("Dữ liệu gửi lên không đúng định dạng JSON.", 400)
        if not isinstance(data, dict):
            return None, ("Dữ liệu gửi lên phải là đối tượng JSON.", 400)
        return data, None

    @app.route("/api/admin/facebook-live/cam<int:cam_id>/approve", methods=["POST"])
    def admin_facebook_live_approve(cam_id):
        if session.get("admin_authenticated") is not True:
            return _no_store_json({"error": "Chỉ quản trị viên được cho phép phát trực tiếp."}, 401)
        if not manager._origin_ok(effective_secure=secure()):
            return _no_store_json({"error": "Nguồn yêu cầu không hợp lệ."}, 403)
        if not manager.enabled() or not manager._meta_ready():
            return _no_store_json({"error": "Chưa bật hoặc cấu hình Meta Page Live Video API."}, 422)
        try:
            seconds = manager.approve(cam_id)
        except ValueError:
            return _no_store_json({"error": "Bàn không khả dụng."}, 403)
        return _no_store_json({"ok": True, "cam_id": cam_id, "approval_seconds": seconds})

    @app.route("/api/admin/facebook-live/cam<int:cam_id>/revoke", methods=["POST"])
    def admin_facebook_live_revoke(cam_id):
        if session.get("admin_authenticated") is not True:
            return _no_store_json({"error": "Chỉ quản trị viên được thu hồi quyền phát."}, 401)
        if not manager._origin_ok(effective_secure=secure()):
            return _no_store_json({"error": "Nguồn yêu cầu không hợp lệ."}, 403)
        try:
            manager.revoke(cam_id)
        except Exception:
            return _no_store_json({"error": "Chưa kết thúc được bài Live trên Meta. Hãy thử lại."}, 503)
        return _no_store_json({"ok": True, "cam_id": cam_id, "approval_seconds": 0})

    @app.route("/api/facebook-live/cam<int:cam_id>/status", methods=["GET"])
    def guest_facebook_live_status(cam_id):
        if not allowed(cam_id):
            return _no_store_json({"error": "Bàn đang tắt hoặc ở chế độ riêng tư."}, 403)
        try:
            result = manager.status(cam_id, session.get("_facebook_live_owner"))
        except Exception:
            return _no_store_json({"error": "Không thể đọc trạng thái phiên phát lúc này."}, 503)
        return _no_store_json(result)

    @app.route("/api/facebook-live/cam<int:cam_id>/start", methods=["POST"])
    def guest_facebook_live_start(cam_id):
        payload, err = _read_json_payload()
        if err:
            return _no_store_json({"error": err[0]}, err[1])

        if (not secure()
                or not manager._origin_ok(effective_secure=True)
                or not manager._valid_nonce(cam_id, request.headers.get("X-CSRF-Token"))):
            return _no_store_json({"error": "Phiên hoặc xác thực trang không hợp lệ."}, 403)

        if payload.get("consent") is not True:
            return _no_store_json({"error": "Cần xác nhận đồng ý phát công khai."}, 400)

        mode = payload.get("mode")
        if mode == "personal":
            if any(k in payload for k in ("phone", "pin", "key", "server", "server_url", "stream_key", "profile_id")):
                return _no_store_json({"error": "Không chấp nhận thông tin nhạy cảm trong yêu cầu phát."}, 400)
            ok, profile_err = _verify_profile_request(cam_id)
            if not ok:
                return _no_store_json({"error": profile_err[0]}, profile_err[1])

            device_token = request.cookies.get(PROFILE_COOKIE_NAME)
            if not device_token:
                return _no_store_json({"error": "Chưa đăng nhập thiết bị cho tài khoản cá nhân."}, 401)

            with manager.lock:
                try:
                    profile = manager.profile_store.authenticate(device_token)
                except ProfileError as pe:
                    return _no_store_json({"error": str(pe)}, pe.status)
                except Exception:
                    return _no_store_json({"error": "Dịch vụ tài khoản cá nhân hiện chưa sẵn sàng."}, 503)

                if not profile or not profile.get("stream_key"):
                    return _no_store_json({"error": "Tài khoản chưa đăng ký Stream Key hoặc phiên đã hết hạn."}, 401)

                try:
                    result, code = manager.start(
                        cam_id=cam_id,
                        owner=session["_facebook_live_owner"],
                        key=profile["stream_key"],
                        mode="personal",
                        profile_id=profile.get("id"),
                        authorization=profile.get("authorization"),
                        expires=profile.get("expires"),
                    )
                except Exception:
                    return _no_store_json({"error": "Không thể bắt đầu phiên phát lúc này."}, 503)
                return _no_store_json(result, code)

        elif mode is not None:
            return _no_store_json({"error": "Chế độ phát sóng không hợp lệ."}, 400)

        # Baseline custom / fanpage mode
        try:
            result, code = manager.start(cam_id, session["_facebook_live_owner"],
                                         payload.get("server"), payload.get("key"))
        except Exception:
            return _no_store_json({"error": "Không thể bắt đầu phiên phát lúc này."}, 503)
        return _no_store_json(result, code)

    @app.route("/api/facebook-live/cam<int:cam_id>/stop", methods=["POST"])
    def guest_facebook_live_stop(cam_id):
        if (not secure()
                or not manager._origin_ok(effective_secure=True)
                or not manager._valid_nonce(cam_id, request.headers.get("X-CSRF-Token"))):
            return _no_store_json({"error": "Phiên hoặc xác thực trang không hợp lệ."}, 403)
        try:
            result, code = manager.stop(cam_id, session["_facebook_live_owner"])
        except Exception:
            return _no_store_json({"error": "Không thể dừng phiên phát lúc này."}, 503)
        return _no_store_json(result, code)

    # ---------------- Cam-scoped Guest Profile Management Endpoints ----------------

    def _verify_profile_request(cam_id):
        if not secure() or not manager._origin_ok(effective_secure=True) or not manager._valid_nonce(cam_id, request.headers.get("X-CSRF-Token")):
            return False, ("Phiên hoặc xác thực trang không hợp lệ.", 403)
        if not manager.enabled():
            return False, ("Quán chưa bật tính năng Facebook Live.", 403)
        if not manager.allowed(cam_id):
            return False, ("Bàn đang tắt hoặc ở chế độ riêng tư.", 403)
        if manager.profile_store is None:
            return False, ("Dịch vụ tài khoản cá nhân hiện chưa sẵn sàng.", 503)
        return True, None

    @app.route("/api/facebook-live/cam<int:cam_id>/profile/status", methods=["POST"])
    def guest_facebook_live_profile_status(cam_id):
        ok, err = _verify_profile_request(cam_id)
        if not ok:
            return _no_store_json({"error": err[0]}, err[1])

        payload, p_err = _read_json_payload()
        if p_err:
            return _no_store_json({"error": p_err[0]}, p_err[1])

        token = request.cookies.get(PROFILE_COOKIE_NAME)
        if not token:
            return _no_store_json({"ok": True, "authenticated": False, "has_key": False})

        with manager.lock:
            try:
                prof = manager.profile_store.authenticate(token)
            except Exception:
                return _no_store_json({"error": "Dịch vụ tài khoản cá nhân hiện chưa sẵn sàng."}, 503)

        if not prof:
            resp = _no_store_json({"ok": True, "authenticated": False, "has_key": False})
            return _clear_device_cookie(resp)

        return _no_store_json({
            "ok": True,
            "authenticated": True,
            "has_key": bool(prof.get("stream_key")),
            "phone": prof.get("phone", ""),
        })

    @app.route("/api/facebook-live/cam<int:cam_id>/profile/register", methods=["POST"])
    def guest_facebook_live_profile_register(cam_id):
        ok, err = _verify_profile_request(cam_id)
        if not ok:
            return _no_store_json({"error": err[0]}, err[1])

        payload, p_err = _read_json_payload()
        if p_err:
            return _no_store_json({"error": p_err[0]}, p_err[1])

        phone, pin, key = payload.get("phone"), payload.get("pin"), payload.get("key")
        if (not isinstance(phone, str) or len(phone) > 32
                or not isinstance(pin, str) or len(pin) > 4
                or not isinstance(key, str) or len(key) > 256):
            return _no_store_json({"error": "Thông tin tài khoản không hợp lệ."}, 400)
        phone, pin, key = phone.strip(), pin.strip(), key.strip()

        if not phone or not pin or not key:
            return _no_store_json({"error": "Vui lòng nhập đầy đủ số điện thoại, mã PIN và Stream Key."}, 400)

        try:
            facebook_target(DEFAULT_FACEBOOK_SERVER, key)
        except ValueError as ve:
            return _no_store_json({"error": str(ve)}, 400)

        trusted_ip = request.remote_addr or ""
        try:
            token = manager.profile_store.register(phone, pin, key, ip=trusted_ip)
        except ProfileError as pe:
            return _no_store_json({"error": str(pe)}, pe.status)
        except Exception:
            return _no_store_json({"error": "Không thể đăng ký tài khoản lúc này."}, 503)

        resp = _no_store_json({
            "ok": True,
            "authenticated": True,
            "has_key": True,
        })
        return _set_device_cookie(resp, token)

    @app.route("/api/facebook-live/cam<int:cam_id>/profile/login", methods=["POST"])
    def guest_facebook_live_profile_login(cam_id):
        ok, err = _verify_profile_request(cam_id)
        if not ok:
            return _no_store_json({"error": err[0]}, err[1])

        payload, p_err = _read_json_payload()
        if p_err:
            return _no_store_json({"error": p_err[0]}, p_err[1])

        phone, pin = payload.get("phone"), payload.get("pin")
        if (not isinstance(phone, str) or len(phone) > 32
                or not isinstance(pin, str) or len(pin) > 4):
            return _no_store_json({"error": "Thông tin đăng nhập không hợp lệ."}, 400)
        phone, pin = phone.strip(), pin.strip()

        if not phone or not pin:
            return _no_store_json({"error": "Vui lòng nhập số điện thoại và mã PIN."}, 400)

        trusted_ip = request.remote_addr or ""
        with manager.lock:
            try:
                token = manager.profile_store.login(
                    phone, pin, ip=trusted_ip, before_rotate=manager.stop_profile_streams)
            except ProfileError as pe:
                return _no_store_json({"error": str(pe)}, pe.status)
            except Exception:
                return _no_store_json({"error": "Đăng nhập thất bại."}, 503)

            resp = _no_store_json({
                "ok": True,
                "authenticated": True,
                "has_key": True,
            })
            return _set_device_cookie(resp, token)

    @app.route("/api/facebook-live/cam<int:cam_id>/profile/update", methods=["POST"])
    def guest_facebook_live_profile_update(cam_id):
        ok, err = _verify_profile_request(cam_id)
        if not ok:
            return _no_store_json({"error": err[0]}, err[1])

        payload, p_err = _read_json_payload()
        if p_err:
            return _no_store_json({"error": p_err[0]}, p_err[1])

        key = payload.get("key")
        if not isinstance(key, str) or len(key) > 256:
            return _no_store_json({"error": "Stream Key không hợp lệ."}, 400)
        key = key.strip()
        if not key:
            return _no_store_json({"error": "Vui lòng cung cấp Stream Key mới."}, 400)

        try:
            facebook_target(DEFAULT_FACEBOOK_SERVER, key)
        except ValueError as ve:
            return _no_store_json({"error": str(ve)}, 400)

        token = request.cookies.get(PROFILE_COOKIE_NAME)
        if not token:
            return _no_store_json({"error": "Chưa đăng nhập thiết bị."}, 401)

        with manager.lock:
            try:
                prof = manager.profile_store.authenticate(token)
                if not prof:
                    resp = _no_store_json({"error": "Phiên đăng nhập đã hết hạn."}, 401)
                    return _clear_device_cookie(resp)

                # Stop all active streams associated with this profile
                manager.stop_profile_streams(prof.get("id"))

                updated = manager.profile_store.update(token, key)
            except ProfileError as pe:
                return _no_store_json({"error": str(pe)}, pe.status)
            except Exception:
                return _no_store_json({"error": "Không thể cập nhật Stream Key."}, 503)

            return _no_store_json({
                "ok": True,
                "authenticated": True,
                "has_key": bool(updated.get("stream_key")),
                "phone": updated.get("phone", prof.get("phone", "")),
            })

    @app.route("/api/facebook-live/cam<int:cam_id>/profile/logout", methods=["POST"])
    def guest_facebook_live_profile_logout(cam_id):
        ok, err = _verify_profile_request(cam_id)
        if not ok:
            return _no_store_json({"error": err[0]}, err[1])

        payload, p_err = _read_json_payload()
        if p_err:
            return _no_store_json({"error": p_err[0]}, p_err[1])

        token = request.cookies.get(PROFILE_COOKIE_NAME)
        if token:
            with manager.lock:
                try:
                    prof = manager.profile_store.authenticate(token)
                    if prof:
                        manager.stop_profile_streams(prof.get("id"))
                        manager.profile_store.revoke(token)
                except Exception:
                    return _no_store_json({"error": "Không thể thu hồi phiên đăng nhập lúc này."}, 503)

        resp = _no_store_json({"ok": True, "authenticated": False, "has_key": False})
        return _clear_device_cookie(resp)

    @app.route("/api/facebook-live/cam<int:cam_id>/profile/delete", methods=["POST"])
    def guest_facebook_live_profile_delete(cam_id):
        ok, err = _verify_profile_request(cam_id)
        if not ok:
            return _no_store_json({"error": err[0]}, err[1])

        payload, p_err = _read_json_payload()
        if p_err:
            return _no_store_json({"error": p_err[0]}, p_err[1])

        token = request.cookies.get(PROFILE_COOKIE_NAME)
        if not token:
            resp = _no_store_json({"error": "Chưa đăng nhập thiết bị."}, 401)
            return _clear_device_cookie(resp)

        with manager.lock:
            try:
                prof = manager.profile_store.authenticate(token)
                if not prof:
                    resp = _no_store_json({"error": "Phiên đăng nhập đã hết hạn."}, 401)
                    return _clear_device_cookie(resp)

                manager.stop_profile_streams(prof.get("id"))
                manager.profile_store.delete(token)
            except ProfileError as pe:
                return _no_store_json({"error": str(pe)}, pe.status)
            except Exception:
                return _no_store_json({"error": "Không thể xóa hồ sơ lúc này."}, 503)

        resp = _no_store_json({"ok": True, "authenticated": False, "has_key": False})
        return _clear_device_cookie(resp)

    return manager
