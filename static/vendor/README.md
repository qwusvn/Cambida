# HEVC browser fallback dependency

Vendored npm package `@kongliu/libde265-web@0.1.4`, unmodified.
Registry: https://registry.npmjs.org/@kongliu/libde265-web/-/libde265-web-0.1.4.tgz
SHA512 integrity: `qd5u/GmcOfh4PWI3SHIRSx6GoLn7F2Q+9ZHPNTlq7+IMTJySvYjNQyNt18ZX0hyXZV59YVaF5OHsWz+RGElEHw==`.

The package directory includes MIT wrapper sources, LGPL libde265 source,
license texts, notices, glue and build scripts for rebuilding/relinking.
See `libde265-web-0.1.4/NOTICE.md` and `docs/licensing.md`. Do not strip these
when distributing the runtime. HEVC patent rights are separate from copyright.

The compatibility page uses the single-threaded WASM backend in a dedicated
Worker; no HTTPS, SharedArrayBuffer, CDN or server transcoding is required.
It supports completed local clips up to 1 GiB, play/pause and keyframe-aligned
seeking; H.264 clips are handed to the browser's native player instead.
Audio, automatic playlist transitions, cut preview and NVR container streaming
are not implemented in this player.

## go2rtc sidecar

The release includes unmodified go2rtc v1.9.15 (MIT), windows/amd64.
Upstream: https://github.com/AlexxIT/go2rtc/releases/tag/v1.9.15
Executable SHA256: `994aa42768d2d6e1bbcf561c85bf9c46cafa705a90a13537cd38be2faee287de`.
Verified against GitHub's SHA256 for `go2rtc_win64.zip`:
`5b448fc90f15c925406d8da0984301985e385050222c1c2bbdf0a76fe4b1e9cb`.
See `go2rtc-LICENSE.txt`. Generated stream configuration is not distributed.
