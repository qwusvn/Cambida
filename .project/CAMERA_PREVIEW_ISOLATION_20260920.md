# Camera preview stream isolation — 2026-09-20

Task: `cambida-camera-preview-isolation-20260920`

## Completed

- Canonical camera settings persist `view_stream` and migrate legacy `netsdk_stream` without retaining conflicting fields.
- NetSDK preview and snapshot use transient runtime clones; recording remains forced to the main stream.
- Admin fields are isolated: Local NetSDK shows no RTSP/NVR advanced fields, Local RTSP shows RTSP fields, and NVR shows NVR fields.
- Raw/legacy NVR values using `ip`, `channel`, `nvr_vendor`, or legacy ports survive browser normalization.

## Verification

- Python unittest: 78/78 PASS.
- Node UI tests: 7/7 PASS.
- Python compile: PASS.
- `git diff --check`: PASS.
- `config.json`, release config, recorded media, and NVR cache were not modified.

## Boundaries

- No Cambida process was listening on port 8004 during this audit, so no service restart or live runtime smoke was performed.
- Real private NetSDK snapshot/live/MP4/ffprobe proof remains blocked by the existing device credential/media boundary; no version bump or release rebuild was made.
