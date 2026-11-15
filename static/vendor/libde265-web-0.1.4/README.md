# libde265-web

English | [简体中文](README.zh-CN.md)

A browser HEVC/H.265 player that prefers WebCodecs and transparently falls
back to libde265 compiled to WebAssembly. Both backends share the same player,
input, frame, and event interfaces.

## Features

- `auto`, `webcodecs`, and `wasm` decoder modes
- Binary WebSocket Annex-B streams with arbitrary message fragmentation
- HEVC NAL/access-unit parsing and VPS/SPS/PPS caching
- Bounded low-latency queues, backpressure events, and keyframe-safe recovery
- A common `UnifiedVideoFrame` interface and optional Canvas rendering
- Dedicated Worker decoding and OffscreenCanvas rendering when supported
- WebGL2 I420/Main10 rendering with a Canvas2D fallback
- Baseline, SIMD, and threads+SIMD WebAssembly builds
- Framework-independent ES modules and generated TypeScript declarations
- No runtime npm dependencies

> libde265 is LGPL-3.0-or-later. The TypeScript code and C++ glue in this
> repository are MIT licensed. Read [Licensing](docs/licensing.md) and
> [Notices](NOTICE.md) before redistribution. HEVC patent obligations require a
> separate review.

## Quick start

```bash
npm install
git submodule update --init --recursive
npm run build
npm run build:wasm
npm run serve
```

Open `http://localhost:8080`. The development server sends COOP/COEP headers so
the threads+SIMD variant can be tested.

```html
<canvas id="video"></canvas>
<script type="module">
  import { createHevcPlayer } from "@kongliu/libde265-web";

  const player = await createHevcPlayer({
    canvas: document.querySelector("#video"),
    mode: "auto",
    codec: "hev1.1.6.L93.B0",
    wasmBaseUrl: "/assets/libde265/",
    wasmWorker: "auto",
    renderer: "auto"
  });

  player.on("error", console.error);
  await player.connect("wss://example.com/hevc-stream");
</script>
```

Deploy every file from `dist/wasm/` at `wasmBaseUrl`. `push()` accepts a raw
Annex-B HEVC byte stream, not MP4, MPEG-TS, FLV, Matroska, or RTP containers.
Demux container data before passing it to the player.

For a custom transport that already provides complete access units and timing:

```js
player.pushAccessUnit(packet.payload, {
  timestamp: packet.ptsUs,
  duration: packet.durationUs,
  key: packet.key,
  sequence: packet.sequence
});
```

## Browser behavior

`auto` calls `VideoDecoder.isConfigSupported()` with the complete codec string
before selecting WebCodecs. HEVC availability depends on the browser, operating
system, installed media components, hardware, and stream profile, so production
deployments should keep the WASM fallback available.

The threads+SIMD variant requires HTTPS or localhost, cross-origin isolation,
and `SharedArrayBuffer`. It is only selected when explicitly requested with
`wasmVariant: "threads-simd"`; automatic selection uses SIMD and then baseline.

For display-only Worker playback, `workerFrameOutput: "metadata"` avoids
copying Y/U/V planes back to the main thread. Keep the default `"planes"` when
application code needs pixel access.

## Low-latency control

The default bounded queue resets the decoder and waits for a new IRAP frame
after severe input pressure. Applications can react to pressure and request a
new random-access point:

```ts
player.on("backpressure", ({ level, stats }) => {
  transport.setPressure(level, stats);
});
player.on("keyframerequest", () => transport.requestIdr());
```

Use `lowLatency: false` or `dropPolicy: "none"` for offline decoding where every
frame must be retained; the caller must then pause the upstream producer.

## Documentation

- [API](docs/api.md)
- [Architecture](docs/architecture.md)
- [Building and deployment](docs/building.md)
- [Licensing](docs/licensing.md)
- [Performance and release validation](docs/performance.md)
- [Troubleshooting](docs/troubleshooting.md)
- [WebSocket input protocol](docs/websocket-protocol.md)

## Validation and packaging

```bash
npm run check
npm test
npm run build
npm run build:wasm
npm run test:wasm
npm run pack
```

`prepack` performs type checks, unit tests, JavaScript compilation, and all
WebAssembly builds. Do not bypass it, because a package assembled from a stale
`dist/wasm` directory could contain an older libde265 binary. The upstream
libde265 v1.1.1 source is pinned as a Git submodule. Re-run the full validation
matrix whenever it or Emscripten changes.

See [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a change. Report
security vulnerabilities according to [SECURITY.md](SECURITY.md), not in a
public issue.
