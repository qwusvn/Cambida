"""Standalone direct-playback viewer for a Hikvision NVR.

This program does not create recordings or read the root project's video folder.
It searches the NVR archive over ISAPI, then remuxes the selected NVR RTSP
playback stream to fragmented MP4 for the browser.  All runtime files stay
under this D: project folder.
"""

import json
import hashlib
import os
import subprocess
import sys
import threading
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import cv2
import requests
from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, send_from_directory
from requests.auth import HTTPDigestAuth


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "nvr_config.json")
LOCAL_FFMPEG = os.path.join(BASE_DIR, "ffmpeg.exe")
SHARED_FFMPEG = os.path.join(os.path.dirname(BASE_DIR), "ffmpeg.exe")
FFMPEG_PATH = LOCAL_FFMPEG if os.path.isfile(LOCAL_FFMPEG) else SHARED_FFMPEG
CACHE_DIR = os.path.join(BASE_DIR, "playback_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
HLS_DIR = os.path.join(BASE_DIR, "playback_hls")
os.makedirs(HLS_DIR, exist_ok=True)

with open(CONFIG_PATH, "r", encoding="utf-8") as config_file:
    CONFIG = json.load(config_file)

NVR = CONFIG["nvr"]
NVR_HOST = str(NVR["host"]).strip()
NVR_HTTP_PORT = int(NVR.get("http_port", 80))
NVR_RTSP_PORT = int(NVR.get("rtsp_port", 554))
NVR_USER = str(NVR["username"])
NVR_PASSWORD = str(NVR["password"])
MAX_SEARCH_HOURS = max(1, min(int(CONFIG.get("max_search_hours", 24)), 24))
MAX_PLAYBACK_MINUTES = max(1, min(int(CONFIG.get("max_playback_minutes", 120)), 240))
ISAPI_BASE = f"http://{NVR_HOST}:{NVR_HTTP_PORT}/ISAPI"
AUTH = HTTPDigestAuth(NVR_USER, NVR_PASSWORD)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

app = Flask(__name__, template_folder=BASE_DIR)
PLAYBACK_CACHE_LOCK = threading.Lock()
HLS_SESSIONS = {}
HLS_LOCK = threading.Lock()

SITE = {
    "name": "ATHENA POOL ROOM",
    "tagline": "Xem lại camera theo bàn",
    "theme": {
        "background": "#2c0b0b",
        "surface": "#660000",
        "primary": "#c1121f",
        "accent": "#ffa500",
        "text": "#ffffff",
    },
}


def xml_name(element):
    return element.tag.rsplit("}", 1)[-1]


def nvr_request(method, path, **kwargs):
    """Authenticated ISAPI request; all callers use read-only endpoints."""
    return requests.request(
        method,
        f"{ISAPI_BASE}{path}",
        auth=AUTH,
        timeout=15,
        **kwargs,
    )


def nvr_local_time():
    reply = nvr_request("GET", "/System/time")
    reply.raise_for_status()
    root = ET.fromstring(reply.content)
    value = next(
        (item.text.strip() for item in root.iter() if xml_name(item) == "localTime" and item.text),
        None,
    )
    if not value:
        raise ValueError("NVR không trả về thời gian hệ thống.")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_time(value, default):
    if not value:
        return default
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default.tzinfo)
    return parsed


def channel_track_id(channel_id):
    if not 1 <= channel_id <= 99:
        raise ValueError("Kênh NVR không hợp lệ.")
    return f"{channel_id}01"


def search_recordings(channel_id, start, end):
    track_id = channel_track_id(channel_id)
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<CMSearchDescription>
  <searchID>{str(uuid.uuid4()).upper()}</searchID>
  <trackIDList><trackID>{track_id}</trackID></trackIDList>
  <timeSpanList><timeSpan>
    <startTime>{start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}</startTime>
    <endTime>{end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}</endTime>
  </timeSpan></timeSpanList>
  <maxResults>200</maxResults>
  <searchResultPostion>0</searchResultPostion>
</CMSearchDescription>'''
    reply = nvr_request(
        "POST",
        "/ContentMgmt/search",
        data=payload.encode("utf-8"),
        headers={"Content-Type": "application/xml; charset=UTF-8"},
    )
    reply.raise_for_status()
    root = ET.fromstring(reply.content)
    if next((item.text for item in root.iter() if xml_name(item) == "responseStatus"), "false") != "true":
        return []

    recordings = []
    for match in (item for item in root.iter() if xml_name(item) == "searchMatchItem"):
        values = {
            xml_name(item): (item.text or "").strip()
            for item in match.iter()
            if xml_name(item) in {"startTime", "endTime", "metadataDescriptor"} and item.text
        }
        if values.get("startTime") and values.get("endTime"):
            recordings.append(
                {
                    "start": values["startTime"],
                    "end": values["endTime"],
                    "type": values.get("metadataDescriptor", "recording").rsplit("/", 1)[-1],
                }
            )
    return recordings


def playback_rtsp_url(channel_id, start, end):
    track_id = channel_track_id(channel_id)
    start_text = start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    end_text = end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    user = quote(NVR_USER, safe="")
    password = quote(NVR_PASSWORD, safe="")
    return (
        f"rtsp://{user}:{password}@{NVR_HOST}:{NVR_RTSP_PORT}/Streaming/tracks/{track_id}/"
        f"?starttime={start_text}&endtime={end_text}"
    )


def live_rtsp_url(channel_id):
    """Use the NVR sub-stream for the preview image in the familiar UI."""
    track_id = f"{channel_id}02"
    user = quote(NVR_USER, safe="")
    password = quote(NVR_PASSWORD, safe="")
    return f"rtsp://{user}:{password}@{NVR_HOST}:{NVR_RTSP_PORT}/Streaming/Channels/{track_id}"


def live_frames(channel_id):
    capture = None
    try:
        capture = cv2.VideoCapture(live_rtsp_url(channel_id))
        while capture.isOpened():
            ok, frame = capture.read()
            if not ok:
                break
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                continue
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + encoded.tobytes() + b"\r\n"
    finally:
        if capture is not None:
            capture.release()


def remux_playback(rtsp_url):
    command = [
        FFMPEG_PATH,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-c",
        "copy",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    try:
        while True:
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        if process.stdout:
            process.stdout.close()


def cached_playback_file(channel_id, start, end):
    """Create a finalized MP4 temporarily so every browser can seek and play it."""
    cache_key = f"{channel_id}|{start.astimezone(timezone.utc).isoformat()}|{end.astimezone(timezone.utc).isoformat()}"
    target = os.path.join(CACHE_DIR, f"nvr_{hashlib.sha256(cache_key.encode()).hexdigest()[:24]}.mp4")
    if os.path.isfile(target) and os.path.getsize(target) > 1024:
        return target

    with PLAYBACK_CACHE_LOCK:
        if os.path.isfile(target) and os.path.getsize(target) > 1024:
            return target
        temporary = f"{target}.part"
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
            duration = max(1, int((end - start).total_seconds()))
            result = subprocess.run(
                [
                    FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
                    "-rtsp_transport", "tcp", "-i", playback_rtsp_url(channel_id, start, end),
                    "-t", str(duration), "-c", "copy", "-movflags", "+faststart", "-f", "mp4", temporary,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=min(900, max(90, duration + 60)),
                creationflags=CREATE_NO_WINDOW,
            )
            if result.returncode != 0 or not os.path.isfile(temporary):
                detail = result.stderr.decode(errors="replace").strip()[-300:]
                raise RuntimeError(detail or "NVR không trả được đoạn video đã chọn.")
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass

        # Keep only a small, disposable cache. It is never part of the recorder archive.
        entries = sorted(
            (os.path.join(CACHE_DIR, name) for name in os.listdir(CACHE_DIR) if name.endswith(".mp4")),
            key=os.path.getmtime,
            reverse=True,
        )
        for old_path in entries[8:]:
            try:
                os.remove(old_path)
            except OSError:
                pass
    return target


def start_hls_playback(channel_id, start, end):
    """Start an HLS session; HLS is supported by Chrome through hls.js."""
    session_id = uuid.uuid4().hex
    session_dir = os.path.join(HLS_DIR, session_id)
    os.makedirs(session_dir, exist_ok=False)
    duration = max(1, int((end - start).total_seconds()))
    playlist = os.path.join(session_dir, "index.m3u8")
    command = [
        FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y", "-rtsp_transport", "tcp",
        "-i", playback_rtsp_url(channel_id, start, end), "-t", str(duration), "-c", "copy",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
        "-hls_segment_filename", os.path.join(session_dir, "segment_%05d.ts"), playlist,
    ]
    process = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    with HLS_LOCK:
        HLS_SESSIONS[session_id] = session_dir
    return session_id


@app.get("/api/nvr/channels")
def api_channels():
    try:
        reply = nvr_request("GET", "/ContentMgmt/InputProxy/channels")
        reply.raise_for_status()
        root = ET.fromstring(reply.content)
        channels = []
        for item in root.iter():
            if xml_name(item) != "InputProxyChannel":
                continue
            channel_id = next((child.text for child in item if xml_name(child) == "id"), None)
            name = next((child.text for child in item if xml_name(child) == "name"), None)
            if channel_id and channel_id.isdigit():
                channels.append({"id": int(channel_id), "name": (name or f"Camera {channel_id}").strip()})
        return jsonify(channels)
    except Exception as exc:
        return jsonify({"error": f"Không kết nối được NVR: {exc}"}), 502


@app.get("/api/nvr/recordings/<int:channel_id>")
def api_recordings(channel_id):
    try:
        now = nvr_local_time()
        start = parse_time(request.args.get("start"), now - timedelta(hours=1))
        end = parse_time(request.args.get("end"), now)
        if end <= start:
            raise ValueError("Thời gian kết thúc phải sau thời gian bắt đầu.")
        if end - start > timedelta(hours=MAX_SEARCH_HOURS):
            raise ValueError(f"Chỉ tìm tối đa {MAX_SEARCH_HOURS} giờ mỗi lần.")
        return jsonify(search_recordings(channel_id, start, end))
    except (ValueError, requests.RequestException, ET.ParseError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/nvr/playback/<int:channel_id>")
def nvr_playback(channel_id):
    try:
        nvr_now = nvr_local_time()
        start = parse_time(request.args.get("start"), nvr_now - timedelta(minutes=5))
        end = parse_time(request.args.get("end"), nvr_now)
        if end <= start or end - start > timedelta(minutes=MAX_PLAYBACK_MINUTES):
            return "Khoảng phát không hợp lệ.", 400
        if not os.path.isfile(FFMPEG_PATH):
            return "Không tìm thấy ffmpeg.exe.", 500
        # Fallback for browsers that accept fragmented MP4 directly.
        response = Response(remux_playback(playback_rtsp_url(channel_id, start, end)), mimetype="video/mp4")
        response.headers["Cache-Control"] = "no-store"
        return response
    except (ValueError, requests.RequestException, ET.ParseError) as exc:
        return f"Không thể mở bản ghi: {exc}", 502


@app.get("/api/nvr/hls/<int:channel_id>")
def nvr_hls_session(channel_id):
    try:
        nvr_now = nvr_local_time()
        start = parse_time(request.args.get("start"), nvr_now - timedelta(minutes=5))
        end = parse_time(request.args.get("end"), nvr_now)
        if end <= start or end - start > timedelta(minutes=MAX_PLAYBACK_MINUTES):
            return jsonify({"error": "Khoảng phát không hợp lệ."}), 400
        return jsonify({"playlist": f"/nvr/hls/{start_hls_playback(channel_id, start, end)}/index.m3u8"})
    except (ValueError, requests.RequestException, ET.ParseError) as exc:
        return jsonify({"error": str(exc)}), 502


@app.get("/nvr/hls/<session_id>/<path:filename>")
def nvr_hls_file(session_id, filename):
    with HLS_LOCK:
        session_dir = HLS_SESSIONS.get(session_id)
    if not session_dir or filename not in {"index.m3u8"} and not filename.startswith("segment_"):
        return "Không tìm thấy phiên phát.", 404
    return send_from_directory(session_dir, filename, mimetype="application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t", conditional=True)


@app.get("/")
def index():
    return redirect("/replay/cam1")


@app.get("/replay/cam<int:channel_id>")
def replay_cam(channel_id):
    try:
        channel_track_id(channel_id)
    except ValueError:
        return "Camera không tồn tại", 404
    return render_template(
        "index.html",
        cam_id=channel_id,
        camera_name=f"Camera {channel_id}",
        site=SITE,
        max_merge_minutes=MAX_PLAYBACK_MINUTES,
    )


@app.get("/cam<int:channel_id>")
def live_camera(channel_id):
    try:
        channel_track_id(channel_id)
    except ValueError:
        return "Camera không tồn tại", 404
    return Response(live_frames(channel_id), mimetype="multipart/x-mixed-replace; boundary=frame")


PAGE = r'''<!doctype html>
<html lang="vi"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Xem lại NVR</title>
<style>body{font:16px system-ui;margin:2rem auto;max-width:850px;background:#170b0b;color:#fff}label,select,input,button{margin:.35rem;padding:.55rem}video{width:100%;background:#000;margin-top:1rem}#status{color:#ffc107}.row{display:flex;flex-wrap:wrap;align-items:center}</style>
<h1>Xem lại trực tiếp từ NVR</h1><p id="status">Đang kết nối NVR…</p>
<div class="row"><label>Camera <select id="channel"></select></label><label>Từ <input id="start" type="datetime-local"></label><label>Đến <input id="end" type="datetime-local"></label><button id="search">Tìm bản ghi</button></div>
<select id="recordings" size="10" style="width:100%"><option>Chọn khoảng thời gian rồi tìm bản ghi.</option></select>
<video id="player" controls playsinline></video>
<script>
const $=id=>document.getElementById(id), pad=n=>String(n).padStart(2,'0');
const localInput=d=>`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
const local=v=>new Date(v).toLocaleString('vi-VN',{hour12:false});
const now=new Date(), ago=new Date(now-3600000); $('start').value=localInput(ago); $('end').value=localInput(now);
async function channels(){const r=await fetch('/api/nvr/channels'),d=await r.json();if(!r.ok)throw Error(d.error);$('channel').innerHTML=d.map(x=>`<option value="${x.id}">${x.name}</option>`).join('');$('status').textContent='Đã kết nối NVR. Chọn thời gian để tìm bản ghi.'}
async function search(){const channel=$('channel').value,start=$('start').value,end=$('end').value;$('status').textContent='Đang tìm bản ghi…';const r=await fetch(`/api/nvr/recordings/${channel}?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`),d=await r.json();if(!r.ok)throw Error(d.error);$('recordings').innerHTML=d.length?d.map(x=>`<option value="${x.start}|${x.end}">${local(x.start)} – ${local(x.end)} (${x.type})</option>`).join(''):'<option>Không có bản ghi trong khoảng đã chọn.</option>';$('status').textContent=`Tìm thấy ${d.length} đoạn.`}
$('search').onclick=()=>search().catch(e=>$('status').textContent=e.message);
$('recordings').onchange=()=>{const [start,end]=$('recordings').value.split('|');if(!end)return;$('player').src=`/nvr/playback/${$('channel').value}?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;$('player').play().catch(()=>{});};
channels().catch(e=>$('status').textContent=e.message);
</script></html>'''


if __name__ == "__main__":
    port = int(CONFIG.get("server_port", 8001))
    app.run(host="0.0.0.0", port=port, debug=False)
