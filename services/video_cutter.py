"""Video cutter & filename helper service for Cambida CCTV.

Handles safe filename resolution, datetime/metadata parsing, and cut output naming.
"""

from datetime import datetime, timedelta
import math
import os
import re
import uuid

TIMELINE_MAX_SECONDS = 24 * 60 * 60
DEFAULT_DURATION = 300
VIDEO_DIR = "cctv_videos"


def _cut_output_filename(source_filename):
    """Keep the source camera identity on new cut files for guest-access checks."""
    match = re.match(r"^(?:merge_|cut_)?cam([1-9][0-9]*)_", source_filename or "", re.IGNORECASE)
    owner = f"cam{match.group(1)}_" if match else ""
    return f"cut_{owner}{uuid.uuid4().hex[:8]}.mp4"


def safe_video_filename(filename):
    """Allow only generated MP4 basenames before using them in a file path."""
    if not isinstance(filename, str) or not filename or len(filename) > 255:
        return False
    if "\x00" in filename or any(char in filename for char in ("/", "\\", ":")):
        return False
    if os.path.basename(filename) != filename:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._()\- ]*\.mp4", filename, re.IGNORECASE))


def safe_video_path(filename, video_dir=None):
    """Resolve a video filename only when it remains below VIDEO_DIR."""
    if not safe_video_filename(filename):
        return None
    target_dir = video_dir if video_dir is not None else VIDEO_DIR
    root = os.path.realpath(target_dir)
    candidate = os.path.realpath(os.path.join(root, filename))
    try:
        if os.path.commonpath((root, candidate)) != root:
            return None
    except ValueError:
        return None
    return candidate


def _valid_camera_id(value):
    """Validate camera id as positive integer up to 10,000."""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, float) and not value.is_integer():
            return None
        if isinstance(value, str) and not re.fullmatch(r"\d+", value.strip()):
            return None
        cam_id = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return cam_id if 1 <= cam_id <= 10_000 else None


def _normalise_duration(value, fallback=None, max_seconds=TIMELINE_MAX_SECONDS):
    """Normalise duration to a valid float within 0 < d <= max_seconds."""
    try:
        duration = float(value)
    except (TypeError, ValueError, OverflowError):
        duration = 0.0
    if not math.isfinite(duration) or duration <= 0:
        try:
            duration = float(fallback)
        except (TypeError, ValueError, OverflowError):
            duration = 0.0
    if not math.isfinite(duration) or not 0 < duration <= max_seconds:
        return None
    return duration


def _parse_local_datetime_value(value):
    """Parse local datetime from string or datetime object, rejecting timezones."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and 0 < len(value.strip()) <= 64:
        raw = value.strip()
        if "T" not in raw and " " not in raw:
            raise ValueError("Thời gian phải là ISO local datetime.")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("Thời gian không đúng định dạng ISO local datetime.") from exc
    else:
        raise ValueError("Thời gian phải là ISO local datetime.")
    if parsed.tzinfo is not None:
        raise ValueError("Timeline chỉ nhận thời gian local, không nhận múi giờ.")
    return parsed.replace(tzinfo=None)


def parse_timeline_range(start_value, end_value, max_seconds=TIMELINE_MAX_SECONDS):
    """Parse and validate timeline start/end range."""
    start = _parse_local_datetime_value(start_value)
    end = _parse_local_datetime_value(end_value)
    duration = (end - start).total_seconds()
    if duration <= 0:
        raise ValueError("Thời gian kết thúc (end) phải sau thời gian bắt đầu (start).")
    if duration > max_seconds:
        raise ValueError("Timeline chỉ được xem tối đa 24 giờ.")
    return start, end


def parse_video_metadata(filename, known_duration=None, fallback_duration=DEFAULT_DURATION, max_seconds=TIMELINE_MAX_SECONDS):
    """Parse supported recording names without probing the media container."""
    if not safe_video_filename(filename):
        return None
    range_match = re.fullmatch(
        r"cam(\d+)_(\d{2})-(\d{2})-(\d{2})_to_"
        r"(\d{2})-(\d{2})-(\d{2})_\((\d{2})-(\d{2})-(\d{4})\)\.mp4",
        filename,
        re.IGNORECASE,
    )
    legacy_match = re.fullmatch(
        r"cam(\d+)_segment_(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})\.mp4",
        filename,
        re.IGNORECASE,
    )
    try:
        if range_match:
            groups = range_match.groups()
            cam_id = _valid_camera_id(groups[0])
            start = datetime.strptime(
                f"{groups[9]}-{groups[8]}-{groups[7]} {groups[1]}:{groups[2]}:{groups[3]}",
                "%Y-%m-%d %H:%M:%S",
            )
            end = datetime.strptime(
                f"{groups[9]}-{groups[8]}-{groups[7]} {groups[4]}:{groups[5]}:{groups[6]}",
                "%Y-%m-%d %H:%M:%S",
            )
            if end == start:
                return None
            if end < start:
                end += timedelta(days=1)
            duration = (end - start).total_seconds()
            source_format = "range"
        elif legacy_match:
            groups = legacy_match.groups()
            cam_id = _valid_camera_id(groups[0])
            start = datetime.strptime(
                f"{groups[1]}-{groups[2]}-{groups[3]} {groups[4]}:{groups[5]}:{groups[6]}",
                "%Y-%m-%d %H:%M:%S",
            )
            duration = _normalise_duration(known_duration, fallback_duration, max_seconds=max_seconds)
            if duration is None:
                return None
            end = start + timedelta(seconds=duration)
            source_format = "segment"
        else:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    if cam_id is None or not 0 < duration <= max_seconds:
        return None
    return {
        "filename": filename,
        "cam_id": cam_id,
        "start": start,
        "end": end,
        "duration_sec": float(duration),
        "format": source_format,
    }


def parse_video_filename(filename, known_duration=None, fallback_duration=DEFAULT_DURATION):
    """Legacy helper returning (start_datetime, filename)."""
    metadata = parse_video_metadata(filename, known_duration=known_duration, fallback_duration=fallback_duration)
    if metadata is None:
        return None, None
    return metadata["start"], filename
