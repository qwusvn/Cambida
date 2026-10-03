"""Live camera streaming service for Cambida CCTV.

Handles low-latency MJPEG frame generation, RTSP buffer draining (Real-time Grab),
and NetSDK frame streaming.
"""

import logging
import os
import time

try:
    import cv2
except ImportError:
    cv2 = None

logger = logging.getLogger(__name__)


def gen_frames(rtsp_url, min_interval=0.08, jpeg_quality=65):
    """Generate MJPEG byte chunks from an RTSP URL with low latency (Zero Buffer Backlog)."""
    if cv2 is None:
        logger.error("[LiveStream] OpenCV (cv2) not available.")
        return

    cap = None
    last_send_time = 0.0
    while True:
        try:
            if cap is None:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                    "rtsp_transport;tcp|fflags;nobuffer|max_delay;500000"
                )
                cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
                if not cap.isOpened():
                    raise ValueError("Không thể mở stream RTSP")
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                last_send_time = 0.0

            # Grab continuously to drain OpenCV RTSP buffer in real-time
            if not cap.grab():
                if cap:
                    cap.release()
                cap = None
                time.sleep(2)
                continue

            now = time.monotonic()
            if now - last_send_time < min_interval:
                continue

            ret, frame = cap.retrieve()
            if not ret or frame is None:
                continue

            last_send_time = now
            ok, buffer = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]
            )
            if not ok:
                continue

            data = buffer.tobytes()
            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                + str(len(data)).encode("ascii")
                + b"\r\n\r\n"
                + data
                + b"\r\n"
            )
        except GeneratorExit:
            # Client closed stream, release immediately
            if cap:
                cap.release()
            return
        except Exception as exc:
            logger.debug("[LiveStream error] %s", exc)
            if cap:
                cap.release()
            cap = None
            time.sleep(2)
