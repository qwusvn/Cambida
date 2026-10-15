"""Meta Graph API for Page Live Video, with bounded HTTP calls and no token logging.

Only administrator-configured Page IDs and Page access tokens are used.
This module does not authenticate arbitrary Facebook users.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import requests


class MetaLiveError(RuntimeError):
    """Public-safe Meta Live API error. Never include Meta response body or credentials."""


_ID = re.compile(r"^[0-9]{4,30}$")
_VERSION = re.compile(r"^v[0-9]{1,2}\.[0-9]{1,2}$")
_ALLOWED_INGEST = ("facebook.com", "fbcdn.net", "facebook.net")
_MAX_TITLE = 160


def valid_page_config(cfg):
    if not isinstance(cfg, dict):
        return False
    page = str(cfg.get("page_id") or "")
    token = str(cfg.get("page_token") or "")
    return bool(_ID.fullmatch(page) and len(token) >= 20 and len(token) <= 1000
                and "\n" not in token and "\r" not in token)


def validate_ingest_url(url):
    """Return Meta's EXACT secure_stream_url; never use an arbitrary client-supplied URL."""
    if not isinstance(url, str) or len(url) > 2048:
        raise MetaLiveError("Đường truyền RTMPS do Meta cung cấp không hợp lệ.")
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        port = p.port
    except ValueError:
        raise MetaLiveError("Đường truyền RTMPS do Meta cung cấp không hợp lệ.") from None
    if (p.scheme != "rtmps" or not any(host == suffix or host.endswith("." + suffix)
                                     for suffix in _ALLOWED_INGEST)
            or p.username or p.password or port not in (None, 443)
            or not p.path.startswith("/rtmp/") or not p.path[6:]
            or p.query or p.fragment or any(ch in url for ch in "\r\n")):
        raise MetaLiveError("Meta trả về đường truyền RTMPS không được phép.")
    return url


def _safe_permalink(url):
    if not isinstance(url, str) or len(url) > 2000:
        return ""
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if (p.scheme == "https" and (host == "facebook.com" or host.endswith(".facebook.com")
                                     or host == "fb.watch") and not p.username and not p.password
                and not p.fragment):
            return url
    except ValueError:
        pass
    return ""


class MetaPageLiveClient:
    def __init__(self, config, http=None, timeout=8):
        self.config = config
        self.http = http or requests
        self.timeout = timeout

    def _credentials(self):
        cfg = self.config() if callable(self.config) else self.config
        if not valid_page_config(cfg):
            raise MetaLiveError("Quán chưa cấu hình Page ID và Page Access Token hợp lệ.")
        version = str(cfg.get("graph_version") or "v26.0")
        if not _VERSION.fullmatch(version):
            raise MetaLiveError("Phiên bản Meta Graph API không hợp lệ.")
        return str(cfg["page_id"]), str(cfg["page_token"]), version

    def _request(self, method, path, payload=None):
        page_id, token, version = self._credentials()
        if not (path == page_id + "/live_videos" or (_ID.fullmatch(path or "") is not None)):
            raise MetaLiveError("Đường dẫn Meta Graph API không hợp lệ.")
        url = f"https://graph.facebook.com/{version}/{path}"
        try:
            response = self.http.request(
                method, url, headers={"Authorization": "Bearer " + token},
                params=payload if method == "GET" else None,
                data=payload if method == "POST" else None,
                timeout=self.timeout, allow_redirects=False,
            )
        except requests.RequestException:
            raise MetaLiveError("Không thể kết nối Meta Graph API.") from None
        if not (200 <= response.status_code < 300):
            raise MetaLiveError("Meta từ chối yêu cầu Live Video. Kiểm tra quyền Page, token và điều kiện phát.")
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise MetaLiveError("Meta trả về dữ liệu không hợp lệ.") from None
        if not isinstance(data, dict) or data.get("error"):
            raise MetaLiveError("Meta Graph API báo lỗi quyền hoặc trạng thái Live Video.")
        return data

    def create_live(self, title):
        page_id, _token, _version = self._credentials()
        safe_title = (str(title or "").strip() or "Cambida - Trực tiếp bàn bida")[:_MAX_TITLE]
        result = self._request("POST", page_id + "/live_videos",
                               {"status": "LIVE_NOW", "title": safe_title,
                                "fields": "id,secure_stream_url,permalink_url"})
        live_id = str(result.get("id") or "")
        if not _ID.fullmatch(live_id):
            raise MetaLiveError("Meta chưa trả về mã Live Video hợp lệ.")
        try:
            ingest = validate_ingest_url(result.get("secure_stream_url"))
        except MetaLiveError:
            # A Page Live object may already exist even when its ingest URL is absent.
            # Best effort close it now; do not expose any potentially unsafe destination.
            try:
                self.end_live(live_id)
            except MetaLiveError:
                pass
            raise
        return {"id": live_id, "ingest": ingest,
                "permalink_url": _safe_permalink(result.get("permalink_url"))}

    def permalink(self, live_id):
        if not _ID.fullmatch(str(live_id or "")):
            return ""
        try:
            result = self._request("GET", str(live_id),
                                   {"fields": "id,permalink_url,status"})
        except MetaLiveError:
            return ""
        return _safe_permalink(result.get("permalink_url"))

    def end_live(self, live_id):
        if not _ID.fullmatch(str(live_id or "")):
            raise MetaLiveError("Mã Live Video không hợp lệ.")
        result = self._request("POST", str(live_id), {"end_live_video": "true"})
        if result.get("success") is not True:
            raise MetaLiveError("Meta chưa xác nhận kết thúc bài Live.")
        return True
