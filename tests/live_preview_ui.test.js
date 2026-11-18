const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');

test('all-camera live view always requests the explicit low-latency sub stream', () => {
  const html = fs.readFileSync('live_all.html', 'utf8');
  assert.match(html, /const feedUrl = \(card\) => `\/cam\$\{card\.dataset\.cameraId\}\?stream=sub&view=live_preview&_=/);
  assert.match(html, /Luồng phụ · độ trễ thấp/);
  assert.doesNotMatch(html, /querySelector\('\.stream'\)|stream=auto|stream=main|\.value = 'main'/);
});

test('live video and MJPEG visibility follow the selected renderer, in both screens', () => {
  const html = fs.readFileSync('index.html', 'utf8');
  // These rules must outrank the shared .player video/.live-stream display:block
  // rule. Otherwise the hidden MJPEG image can cover a working WebRTC video.
  assert.match(html, /\.player video\[hidden\], \.player \.live-stream\[hidden\]\s*\{\s*display:\s*none;\s*\}/);
  assert.doesNotMatch(html, /\.screen\.is-live \.player video\s*\{\s*display:\s*none/);
  assert.doesNotMatch(html, /\.screen\.is-live \.live-stream\s*\{\s*display:\s*block/);
  assert.match(html, /video\.hidden=false;\s*stream\.hidden=true;\s*watchFrames\(\)/);
  assert.match(html, /video\.srcObject=null;video\.hidden=true;\s*stream\.src=/);
  assert.match(html, /stream\.onload=\(\)=>\{[\s\S]*?stream\.hidden=false;/);
  assert.match(html, /if\(isLive\)\{disposeHevc\(video\);video\._hevcPending=false;video\._nativeAudio=false;video\.muted=true;/);
  assert.match(html, /stream\.onerror=\(\)=>\{\s*if\(!screenEl\.classList\.contains\("is-live"\)\|\|!stream\.getAttribute\("src"\)\)return;/);
  for (const id of ['videoPlayer', 'cutPlayer', 'liveStream', 'cutLiveStream']) {
    assert.ok(html.includes(`id="${id}"`), `missing renderer ${id}`);
  }
});
