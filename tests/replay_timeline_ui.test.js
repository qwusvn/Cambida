const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

function makeElement(id = '') {
  const listeners = new Map();
  const classes = new Set();
  return {
    id,
    value: '',
    paused: true,
    readyState: 0,
    currentTime: 0,
    duration: 60,
    src: '',
    style: {},
    dataset: {},
    children: [],
    textContent: '',
    title: '',
    className: '',
    classList: {
      add: (...names) => names.forEach(name => classes.add(name)),
      remove: (...names) => names.forEach(name => classes.delete(name)),
      contains: name => classes.has(name),
      toggle: (name, force) => {
        const next = force === undefined ? !classes.has(name) : Boolean(force);
        if (next) classes.add(name); else classes.delete(name);
        return next;
      },
    },
    appendChild(child) { this.children.push(child); },
    addEventListener(type, handler) {
      const current = listeners.get(type) || [];
      current.push(handler);
      listeners.set(type, current);
    },
    removeEventListener(type, handler) {
      const current = listeners.get(type) || [];
      listeners.set(type, current.filter(candidate => candidate !== handler));
    },
    dispatch(type, event = {}) {
      for (const handler of listeners.get(type) || []) handler({ type, target: this, ...event });
    },
    setAttribute() {},
    removeAttribute() {},
    closest() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { left: 0, right: 100, width: 100, top: 0, height: 100 }; },
    setPointerCapture() {},
    hasPointerCapture() { return false; },
    releasePointerCapture() {},
    play() { this.paused = false; return Promise.resolve(); },
    pause() { this.paused = true; },
    load() {},
  };
}

function loadReplayScript(cameraMode = 'nvr') {
  const html = fs.readFileSync('index.html', 'utf8');
  const start = html.indexOf('<script>') + '<script>'.length;
  const end = html.lastIndexOf('</script>');
  if (start < '<script>'.length || end < start) throw new Error('replay script missing');

  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  };
  element('filterDate').value = '2026-09-20';
  const document = {
    getElementById: element,
    createElement: () => makeElement(),
    querySelectorAll: () => [],
    addEventListener() {},
  };
  const window = { addEventListener() {}, scrollTo() {} };
  let timer = null;
  const context = {
    console,
    document,
    window,
    fetch: async () => ({ ok: true, json: async () => [] }),
    EventSource: class EventSource {},
    requestAnimationFrame: () => 0,
    setTimeout: (callback, delay) => { timer = { callback, delay }; return 1; },
    clearTimeout: () => { timer = null; },
    setInterval: () => 1,
    clearInterval: () => {},
  };
  vm.createContext(context);
  const source = html
    .slice(start, end)
    .replaceAll('{{ camera_mode }}', cameraMode)
    .replaceAll('{{ cam_id }}', '1')
    .replaceAll('{{ max_merge_minutes|int }}', '60');
  new vm.Script(source, { filename: 'index.html' }).runInContext(context);
  return { context, elements, getTimer: () => timer };
}

test('timeline coverage merges segmentation jitter but preserves real gaps', () => {
  const { context, elements } = loadReplayScript();
  vm.runInContext(`visibleVideos = [
    { started_at: '2026-09-20T00:00:00', end_at: '2026-09-20T00:10:00' },
    { started_at: '2026-09-20T00:10:01', end_at: '2026-09-20T00:20:00' },
    { started_at: '2026-09-20T00:20:06', end_at: '2026-09-20T00:30:00' },
  ];`, context);

  context.renderTimelineCoverage('filmstrip');
  const cells = elements.get('filmstrip').children;
  assert.equal(cells.length, 2);
  assert.match(cells[0].title, /00:00:00 - 00:10:00/);
  assert.match(cells[0].title, /00:10:01 - 00:20:00/);
  assert.doesNotMatch(cells[0].title, /00:20:06/);
  assert.match(cells[1].title, /00:20:06 - 00:30:00/);
  assert.ok(Number.parseFloat(cells[0].style.width) > 1);
  assert.ok(Number.parseFloat(cells[1].style.left) > Number.parseFloat(cells[0].style.left));
});

test('video overlays are removed and controls stay in the external toolbar', () => {
  const html = fs.readFileSync('index.html', 'utf8');
  const { context, elements } = loadReplayScript();
  elements.set('playerFrame', makeElement('playerFrame'));
  const frame = elements.get('playerFrame');
  elements.get('pauseButton');
  elements.get('pauseIcon');
  elements.get('playerTime');
  elements.get('fullscreenButton');

  context.wirePlayer(
    'videoPlayer',
    'pauseButton',
    'pauseIcon',
    'playerTime',
    'playerFrame',
    'fullscreenButton',
    'replay',
  );
  frame.dispatch('pointerdown', { pointerType: 'mouse' });
  assert.equal(frame.classList.contains('is-controls-visible'), false);
  assert.doesNotMatch(html, /player-top|player-controls|is-controls-visible|CONTROL_IDLE_MS/);
  assert.doesNotMatch(html, /id="(?:overlayTimestamp|cutOverlayTimestamp)"/);
  assert.match(html, /<\/div>\s*<div class="player-toolbar"/);
  assert.match(html, /id="pauseButton" class="player-toolbar-button"/);
  assert.match(html, /id="fullscreenButton" class="player-toolbar-button fullscreen-button"/);
});

test('playback rate controls are single-direction cyclers and zoom sits in the top toolbar', () => {
  const html = fs.readFileSync('index.html', 'utf8');
  const { context } = loadReplayScript();
  assert.equal((html.match(/data-timeline-context="replay" data-direction="reverse"/g) || []).length, 1);
  assert.equal((html.match(/data-timeline-context="replay" data-direction="forward"/g) || []).length, 1);
  assert.equal((html.match(/data-timeline-context="cut" data-direction="reverse"/g) || []).length, 1);
  assert.equal((html.match(/data-timeline-context="cut" data-direction="forward"/g) || []).length, 1);
  assert.match(html, /data-direction="reverse" data-rate="1"[^>]*>[\s\S]*?<span class="rate-label">1x<\/span>/);
  assert.match(html, /data-direction="forward" data-rate="1"[^>]*>[\s\S]*?<span class="rate-label">1x<\/span>/);
  assert.deepEqual([1, 2, 4, 1], [1, context.getNextPlaybackRate(1), context.getNextPlaybackRate(2), context.getNextPlaybackRate(4)]);
  assert.ok(html.indexOf('id="timelineZoomOut"') < html.indexOf('id="timelineViewport"'));
  assert.ok(html.indexOf('id="cutTimelineZoomOut"') < html.indexOf('id="cutTimelineViewport"'));
});

test('live mode keeps timeline visible and jumps it to the current day/time', () => {
  const html = fs.readFileSync('index.html', 'utf8');
  const { context, elements } = loadReplayScript();
  assert.doesNotMatch(html, /\.screen\.is-live \.timeline-wrap/);
  elements.get('filterDate').value = '2026-09-19';
  const progress = context.jumpTimelineToNow('replay');
  assert.equal(elements.get('filterDate').value, context.fmtDateInput(new Date()));
  assert.equal(elements.get('cutFilterDate').value, elements.get('filterDate').value);
  assert.ok(progress >= 0 && progress <= 1);
  assert.equal(vm.runInContext('timelineStates.replay.progress', context), progress);
});

test('live click uses the current /cam stream without loading recorded playback', () => {
  const { context, elements } = loadReplayScript();
  elements.set('replayScreen', makeElement('replayScreen'));
  elements.set('liveStream', makeElement('liveStream'));
  vm.runInContext("document.getElementById('videoPlayer')", context);

  context.setScreenLive('replay', true);

  assert.match(elements.get('liveStream').src, /^\/cam1\?stream=sub&view=live_preview&live=/);
  assert.equal(elements.get('videoPlayer').src, '');
  assert.equal(elements.get('replayScreen').classList.contains('is-live'), true);
});

test('cut live mode uses the same explicit low-latency sub preview stream', () => {
  const { context, elements } = loadReplayScript();
  elements.set('cutScreen', makeElement('cutScreen'));
  elements.set('cutLiveStream', makeElement('cutLiveStream'));
  elements.set('cutPlayer', makeElement('cutPlayer'));
  elements.set('cutLiveButton', makeElement('cutLiveButton'));
  elements.set('cutLiveButtonText', makeElement('cutLiveButtonText'));
  elements.set('cutPlayerFrame', makeElement('cutPlayerFrame'));
  context.setScreenLive('cut', true);
  assert.match(elements.get('cutLiveStream').src, /^\/cam1\?stream=sub&view=live_preview&live=/);
  assert.equal(elements.get('cutScreen').classList.contains('is-live'), true);
});

test('playback direction buttons cycle active speed 1x to 2x to 4x to 1x', () => {
  const { context, elements } = loadReplayScript();
  vm.runInContext("currentVideo={started_at:'2026-09-20T00:00:00',end_at:'2026-09-20T00:10:00'}", context);
  const forward = makeElement('forwardRate');
  const forwardLabel = makeElement('forwardLabel');
  forward.dataset = { direction: 'forward', rate: '1' };
  forward.querySelector = selector => selector === '.rate-label' ? forwardLabel : null;
  context.setPlaybackRate('replay', 'forward', forward);
  const player = elements.get('videoPlayer');
  assert.equal(forward.dataset.rate, '1');
  assert.equal(player.playbackRate, 1);
  context.setPlaybackRate('replay', 'forward', forward);
  assert.equal(forward.dataset.rate, '2');
  assert.equal(player.playbackRate, 2);
  context.setPlaybackRate('replay', 'forward', forward);
  assert.equal(forward.dataset.rate, '4');
  context.setPlaybackRate('replay', 'forward', forward);
  assert.equal(forward.dataset.rate, '1');
  assert.equal(forwardLabel.textContent, '1x');
});

test('manual replay scrub selects the exact recording and starts playback', () => {
  const { context, elements } = loadReplayScript();
  vm.runInContext(`visibleVideos = [
    { name: 'cam1', started_at: '2026-09-20T00:00:00', end_at: '2026-09-20T23:59:59' }
  ]; currentVideo = null;`, context);

  context.seekTimelineProgress('replay', 0.5, true, true);

  const player = elements.get('videoPlayer');
  assert.equal(player.paused, false);
  assert.match(player.src, /at=2026-09-20T12%3A00%3A00/);
});

test('manual scrub retries autoplay after the selected media becomes playable', async () => {
  const { context, elements } = loadReplayScript();
  vm.runInContext(`visibleVideos = [
    { name: 'cam1', started_at: '2026-09-20T00:00:00', end_at: '2026-09-20T23:59:59' }
  ]; currentVideo = null;`, context);

  vm.runInContext("document.getElementById('videoPlayer')", context);
  const player = elements.get('videoPlayer');
  let playAttempts = 0;
  player.play = () => {
    playAttempts += 1;
    if (player.readyState < 3) return Promise.reject(new Error('media not ready'));
    player.paused = false;
    return Promise.resolve();
  };

  context.seekTimelineProgress('replay', 0.5, true, true);
  await Promise.resolve();
  assert.equal(player.paused, true);
  assert.equal(playAttempts, 1);

  player.readyState = 3;
  player.dispatch('canplay');
  await Promise.resolve();
  assert.equal(player.paused, false);
  assert.equal(playAttempts, 2);
});

test('stale loadedmetadata callbacks cannot override the latest scrub target', () => {
  const { context, elements } = loadReplayScript('local');
  vm.runInContext(`visibleVideos = [
    { name: 'cam1', source: 'local', started_at: '2026-09-20T00:00:00', end_at: '2026-09-20T01:00:00' }
  ]; currentVideo = visibleVideos[0]; currentVideoIndex = 0;`, context);
  vm.runInContext("document.getElementById('videoPlayer').duration = 3600", context);

  context.seekTimelineProgress('replay', 5 / 24 / 60, false, false);
  context.seekTimelineProgress('replay', 25 / 24 / 60, false, false);
  const player = elements.get('videoPlayer');
  player.dispatch('loadedmetadata');

  assert.equal(player.currentTime, 25 * 60);
});

test('live list refresh does not load replay, but a user scrub exits live and plays replay', () => {
  const { context, elements } = loadReplayScript();
  elements.set('replayScreen', makeElement('replayScreen'));
  vm.runInContext("document.getElementById('videoPlayer')", context);
  vm.runInContext(`allVideos = [
    { name: 'cam1', started_at: '2026-09-20T00:00:00', end_at: '2026-09-20T23:59:59' }
  ]; visibleVideos = allVideos; currentVideo = null;`, context);
  elements.get('replayScreen').classList.add('is-live');
  context.updateVisibleVideos();
  assert.equal(context.currentVideo, undefined);
  assert.equal(elements.get('videoPlayer').src, '');

  context.seekTimelineProgress('replay', 0.5, true, true);
  assert.equal(elements.get('replayScreen').classList.contains('is-live'), false);
  assert.equal(elements.get('videoPlayer').paused, false);
});
