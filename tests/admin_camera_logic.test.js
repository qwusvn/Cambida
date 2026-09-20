const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

function loadAdminScript() {
  const html = fs.readFileSync('admin.html', 'utf8');
  const start = html.indexOf('<script>') + '<script>'.length;
  const end = html.lastIndexOf('</script>');
  if (start < '<script>'.length || end < start) throw new Error('admin script missing');

  const elements = new Map();
  const makeElement = () => ({
    value: '',
    checked: false,
    innerHTML: '',
    textContent: '',
    hidden: false,
    style: {},
    dataset: {},
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener() {},
    setAttribute() {},
    removeAttribute() {},
    closest() { return null; },
    getBoundingClientRect() { return { left: 0, right: 100, width: 100, top: 0, height: 100 }; },
    play() { return Promise.resolve(); },
    pause() {},
  });
  const element = id => {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  };
  const body = element('body');
  const document = {
    body,
    getElementById: element,
    querySelectorAll: () => [],
    addEventListener() {},
  };
  const window = {
    addEventListener() {},
    scrollTo() {},
  };
  const localStorage = { getItem: () => null, setItem() {} };
  const fetch = async url => {
    let payload = [];
    if (String(url).includes('/api/admin/config')) payload = { cameras: [], tables: [] };
    else if (String(url).includes('/api/status')) payload = { cameras: [], video_used_gb: 0, video_limit_gb: 1, disk_free_gb: 1, orphan_part_files: 0, video_counts: {} };
    return { ok: true, json: async () => payload };
  };
  const context = {
    console,
    document,
    window,
    localStorage,
    fetch,
    FormData: class FormData {},
    confirm: () => true,
    prompt: () => '',
    setInterval: () => 0,
    setTimeout,
    clearTimeout,
  };
  vm.createContext(context);
  new vm.Script(html.slice(start, end), { filename: 'admin.html' }).runInContext(context);
  return { context, elements };
}

test('admin camera renderer isolates NetSDK, RTSP, and zero-camera states', () => {
  const { context, elements } = loadAdminScript();
  assert.equal(typeof context.normalizeCamera, 'function');
  assert.equal(typeof context.setForm, 'function');

  const stale = {
    name: 'Unit camera',
    playback_source: 'local',
    local_transport: 'netsdk',
    ip: '192.0.2.10',
    user: 'unit-user',
    pass: 'unit-pass',
    port: 554,
    record_path: 'stale/main',
    preview_path: 'stale/sub',
    record_rtsp_url: 'rtsp://stale/record',
    preview_rtsp_url: 'rtsp://stale/preview',
    netsdk_port: 37777,
    netsdk_channel: 1,
    netsdk_stream: 'main',
  };
  const netsdk = context.normalizeCamera(stale, 0);
  assert.equal(netsdk.local_transport, 'netsdk');
  for (const key of ['port', 'record_path', 'preview_path', 'record_rtsp_url', 'preview_rtsp_url']) {
    assert.equal(Object.hasOwn(netsdk, key), false, `NetSDK retained ${key}`);
  }

  context.setForm({ cameras: [stale], tables: [] });
  const netsdkMarkup = elements.get('cameraList').innerHTML;
  assert.match(netsdkMarkup, /NetSDK port/);
  assert.doesNotMatch(netsdkMarkup, /Preset RTSP camera|Chỉnh luồng RTSP camera nâng cao|cam-record-path/);
  assert.doesNotMatch(netsdkMarkup, /Cấu hình đầu ghi nâng cao|cam-nvr-timezone|cam-nvr-connect-timeout|cam-nvr-https/);

  context.setForm({ cameras: [{ ...stale, local_transport: 'rtsp' }], tables: [] });
  const rtspMarkup = elements.get('cameraList').innerHTML;
  assert.match(rtspMarkup, /Preset RTSP camera/);
  assert.match(rtspMarkup, /Chỉnh luồng RTSP camera nâng cao/);
  assert.doesNotMatch(rtspMarkup, /NetSDK port/);

  context.setForm({ cameras: [], tables: [] });
  assert.match(elements.get('cameraList').innerHTML, /Chưa có kênh/);
  assert.match(elements.get('qrGrid').innerHTML, /Chưa có camera/);

  const legacyNvr = context.normalizeCamera({
    name: 'Legacy NVR',
    playback_source: 'nvr',
    ip: '192.0.2.20',
    nvr_vendor: 'dahua',
    channel: 4,
    port: 8554,
    username: 'legacy-admin',
    password: 'legacy-pass',
  }, 0);
  assert.equal(legacyNvr.host, '192.0.2.20');
  assert.equal(legacyNvr.vendor, 'dahua');
  assert.equal(legacyNvr.nvr_channel, 4);
  assert.equal(legacyNvr.rtsp_port, 8554);
});

test('admin camera normalizes and renders view_stream (auto/main/sub)', () => {
  const { context, elements } = loadAdminScript();

  // Default fallback is auto
  const c1 = context.normalizeCamera({ name: 'Cam 1', ip: '1.2.3.4' }, 0);
  assert.equal(c1.view_stream, 'auto');

  // Explicit values
  const c2 = context.normalizeCamera({ name: 'Cam 2', ip: '1.2.3.4', view_stream: 'sub' }, 1);
  assert.equal(c2.view_stream, 'sub');

  const c3 = context.normalizeCamera({ name: 'Cam 3', ip: '1.2.3.4', view_stream: 'main' }, 2);
  assert.equal(c3.view_stream, 'main');

  // Legacy netsdk_stream mapped if view_stream absent; netsdk_stream is not retained
  const cLegacy = context.normalizeCamera({ name: 'Cam Legacy', ip: '1.2.3.4', local_transport: 'netsdk', netsdk_stream: 'sub' }, 3);
  assert.equal(cLegacy.view_stream, 'sub');
  assert.equal(Object.hasOwn(cLegacy, 'netsdk_stream'), false);

  const cLegacyMain = context.normalizeCamera({ name: 'Cam Legacy Main', ip: '1.2.3.4', local_transport: 'netsdk', netsdk_stream: 'main' }, 4);
  assert.equal(cLegacyMain.view_stream, 'main');
  assert.equal(Object.hasOwn(cLegacyMain, 'netsdk_stream'), false);

  // Stale legacy netsdk_stream cannot override valid view_stream
  const cStale1 = context.normalizeCamera({ name: 'Cam Stale 1', ip: '1.2.3.4', local_transport: 'netsdk', view_stream: 'sub', netsdk_stream: 'main' }, 5);
  assert.equal(cStale1.view_stream, 'sub');
  assert.equal(Object.hasOwn(cStale1, 'netsdk_stream'), false);

  const cStale2 = context.normalizeCamera({ name: 'Cam Stale 2', ip: '1.2.3.4', local_transport: 'netsdk', view_stream: 'auto', netsdk_stream: 'sub' }, 6);
  assert.equal(cStale2.view_stream, 'auto');
  assert.equal(Object.hasOwn(cStale2, 'netsdk_stream'), false);

  const cStale3 = context.normalizeCamera({ name: 'Cam Stale 3', ip: '1.2.3.4', local_transport: 'netsdk', view_stream: 'main', netsdk_stream: 'sub' }, 7);
  assert.equal(cStale3.view_stream, 'main');
  assert.equal(Object.hasOwn(cStale3, 'netsdk_stream'), false);

  // NVR camera preserves view_stream
  const cNvr = context.normalizeCamera({ name: 'NVR Cam', playback_source: 'nvr', host: '1.2.3.5', view_stream: 'sub' }, 8);
  assert.equal(cNvr.view_stream, 'sub');

  // Markup includes cam-view-stream selector
  context.setForm({ cameras: [c1, c2, cNvr], tables: [] });
  const markup = elements.get('cameraList').innerHTML;
  assert.match(markup, /class="form-select cam-view-stream"/);
  assert.match(markup, /Luồng xem trực tiếp/);
  assert.match(markup, /value="auto"/);
  assert.match(markup, /value="main"/);
  assert.match(markup, /value="sub"/);
});
