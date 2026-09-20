const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');

test('all-camera live view always requests the explicit low-latency sub stream', () => {
  const html = fs.readFileSync('live_all.html', 'utf8');
  assert.match(html, /const feedUrl = \(card\) => `\/cam\$\{card\.dataset\.cameraId\}\?stream=sub&view=live_preview&_=/);
  assert.match(html, /Luồng phụ · độ trễ thấp/);
  assert.doesNotMatch(html, /querySelector\('\.stream'\)|stream=auto|stream=main|\.value = 'main'/);
});
