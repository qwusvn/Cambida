"""Network and tunnel helper service for Cambida CCTV.

Handles local LAN IP detection, public base URL resolution, and Cloudflare tunnel status.
"""

import json
import logging
import os
import socket
import time
import requests

logger = logging.getLogger(__name__)

_TUNNEL_ONLINE = True
_TUNNEL_LAST_CHECK = 0
_TUNNEL_CHECK_INTERVAL = 15


def get_local_lan_ip():
    """Detect the private LAN IPv4 of this host, avoiding loopback."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        host_name = socket.gethostname()
        ip = socket.gethostbyname(host_name)
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


def get_public_base_url(config_file=None, config_dict=None):
    """Retrieve public base URL (HTTPS) from config file or config dictionary."""
    if config_file and os.path.isfile(config_file):
        try:
            with open(config_file, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
                val = (cfg.get("public_base_url") or "").strip().rstrip("/")
                if val:
                    if config_dict is not None:
                        config_dict["public_base_url"] = val
                    return val
        except Exception:
            pass
    if config_dict is not None:
        return (config_dict.get("public_base_url") or "").strip().rstrip("/")
    return ""


def is_tunnel_online(public_base=None):
    """Check if Cloudflare HTTPS tunnel is considered online."""
    global _TUNNEL_ONLINE
    if not public_base:
        return False
    return _TUNNEL_ONLINE


def tunnel_health_loop(get_url_fn, interval=15):
    """Background worker monitoring public tunnel reachability."""
    global _TUNNEL_ONLINE, _TUNNEL_LAST_CHECK
    time.sleep(2)
    while True:
        try:
            public_base = get_url_fn()
            if public_base:
                try:
                    resp = requests.get(f"{public_base}/api/ping", timeout=2.5)
                    _TUNNEL_ONLINE = (resp.status_code == 200)
                except Exception:
                    _TUNNEL_ONLINE = False
            else:
                _TUNNEL_ONLINE = False
            _TUNNEL_LAST_CHECK = time.time()
        except Exception:
            _TUNNEL_ONLINE = False
        time.sleep(interval)
