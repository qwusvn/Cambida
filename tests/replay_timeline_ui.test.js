const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

function makeElement(id = '') {
  const listeners = new Map();
  const classes = new Set();
  let text = '';
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
    get textContent() {
      return text || this.children.map(c => c.textContent).join('');
    },
    set textContent(val) {
      text = String(val);
    },
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
    querySelector: () => null,
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
  assert.ok(Number.parseFloat(cells[0].style.width) > 0.5);
  assert.ok(Number.parseFloat(cells[1].style.left) > Number.parseFloat(cells[0].style.left));
});

test('player controls are streamlined with overlay fullscreen and no external playback bar', () => {
  const html = fs.readFileSync('index.html', 'utf8');
  const { context, elements } = loadReplayScript();
  elements.set('playerFrame', makeElement('playerFrame'));
  const frame = elements.get('playerFrame');
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
  assert.doesNotMatch(html, /id="pauseButton"|id="playerTime"/);
  assert.match(html, /id="fullscreenButton" class="video-overlay-button"/);
});

test('playback rate controls sit together on the left and zoom buttons are restored defaulting to 5 hours', () => {
  const html = fs.readFileSync('index.html', 'utf8');
  const { context } = loadReplayScript();
  assert.equal((html.match(/data-timeline-context="replay" data-direction="reverse"/g) || []).length, 1);
  assert.equal((html.match(/data-timeline-context="replay" data-direction="forward"/g) || []).length, 1);
  assert.equal((html.match(/data-timeline-context="cut" data-direction="reverse"/g) || []).length, 1);
  assert.equal((html.match(/data-timeline-context="cut" data-direction="forward"/g) || []).length, 1);
  assert.match(html, /data-direction="reverse" data-rate="1"[^>]*>[\s\S]*?<span class="rate-label">1X<\/span>/);
  assert.match(html, /data-direction="forward" data-rate="1"[^>]*>[\s\S]*?<span class="rate-label">1X<\/span>/);
  assert.deepEqual([2, 4, 1], [context.getNextPlaybackRate(1), context.getNextPlaybackRate(2), context.getNextPlaybackRate(4)]);
  assert.ok(html.includes('id="timelineZoomOut"'));
  assert.ok(html.includes('id="cutTimelineZoomOut"'));
  assert.ok(Math.abs(context.DEFAULT_TIMELINE_ZOOM - 9.6) < 0.1);
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

test('playback direction buttons cycle active speed immediately on first click (2x -> 4x -> 1x -> 2x)', () => {
  const { context, elements } = loadReplayScript();
  vm.runInContext("currentVideo={started_at:'2026-09-20T00:00:00',end_at:'2026-09-20T00:10:00'}", context);
  const forward = makeElement('forwardRate');
  const forwardLabel = makeElement('forwardLabel');
  forward.dataset = { direction: 'forward', rate: '1' };
  forward.querySelector = selector => selector === '.rate-label' ? forwardLabel : null;
  context.setPlaybackRate('replay', 'forward', forward);
  const player = elements.get('videoPlayer');
  assert.equal(forward.dataset.rate, '2');
  assert.equal(player.playbackRate, 2);
  context.setPlaybackRate('replay', 'forward', forward);
  assert.equal(forward.dataset.rate, '4');
  assert.equal(player.playbackRate, 4);
  context.setPlaybackRate('replay', 'forward', forward);
  assert.equal(forward.dataset.rate, '1');
  assert.equal(player.playbackRate, 1);
  context.setPlaybackRate('replay', 'forward', forward);
  assert.equal(forward.dataset.rate, '2');
  assert.equal(player.playbackRate, 2);
});

test('manual replay scrub selects the exact recording and starts playback', () => {
  const { context, elements } = loadReplayScript();
  vm.runInContext(`visibleVideos = [
    { name: 'cam1', started_at: '2026-09-20T00:00:00', end_at: '2026-09-20T23:59:59' }
  ]; currentVideo = null;`, context);

  context.seekTimelineProgress('replay', 0.75, true, true);

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

  context.seekTimelineProgress('replay', 0.75, true, true);
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

  context.seekTimelineProgress('replay', 0.5 + 5 / 48 / 60, false, false);
  context.seekTimelineProgress('replay', 0.5 + 25 / 48 / 60, false, false);
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

  context.seekTimelineProgress('replay', 0.75, true, true);
  assert.equal(elements.get('replayScreen').classList.contains('is-live'), false);
  assert.equal(elements.get('videoPlayer').paused, false);
});

test('timeline ruler renders continuous 48-hour seamless markers with midnight transition', () => {
  const { context, elements } = loadReplayScript();
  context.updateRuler('ruler');
  const children = elements.get('ruler').children;
  assert.equal(children.length, 49);
  assert.equal(children[0].textContent, 'Hôm trước');
  assert.equal(children[23].textContent, '23h');
  assert.equal(children[24].textContent, '24h0h');
  assert.equal(children[25].textContent, '1h');
  assert.equal(children[26].textContent, '2h');
  assert.equal(children[48].textContent, '24h');
});

test('previous-day navigation keeps replay and cut on date-correct absolute times', () => {
  const { context, elements } = loadReplayScript();
  elements.get('filterDate').value = '2026-09-20';
  elements.set('cutFilterDate', makeElement('cutFilterDate'));
  elements.get('cutFilterDate').value = '2026-09-20';
  context.changeTimelineDay(-1);
  assert.equal(elements.get('filterDate').value, '2026-09-19');
  assert.equal(elements.get('cutFilterDate').value, '2026-09-19');
  assert.equal(context.formatLocalSecond(context.getTimelineDateAtProgress(47 / 48)), '2026-09-19T23:00:00');
  context.changeTimelineDay(1);
  assert.equal(context.formatLocalSecond(context.getTimelineDateAtProgress(25 / 48)), '2026-09-20T01:00:00');
});

test('action buttons feature cut video as primary and download button is removed', () => {
  const html = fs.readFileSync('index.html', 'utf8');
  assert.doesNotMatch(html, /id="downloadOriginalBtn"/);
  assert.match(html, /id="cutButton" class="action primary cut-primary"[^>]*>[\s\S]*?Cắt Video<\/button>/);
});

test('dragging timeline past 0h to the left rolls over into previous day', () => {
  const { context, elements } = loadReplayScript();
  elements.get('filterDate').value = '2026-09-20';
  elements.set('cutFilterDate', makeElement('cutFilterDate'));
  elements.get('cutFilterDate').value = '2026-09-20';
  vm.runInContext(`visibleVideos = [
    { name: 'cam1', started_at: '2026-09-19T00:00:00', end_at: '2026-09-20T23:59:59' }
  ]; currentVideo = null;`, context);

  context.timelineStates.replay.zoom = 1; context.wireTimeline('timelineHit', false);
  const hit = context.document.getElementById('timelineHit');
  const viewport = context.document.getElementById('timelineViewport');
  viewport.getBoundingClientRect = () => ({ width: 1000, left: 0, right: 1000, top: 0, bottom: 50, height: 50 });

  hit.dispatch('pointerdown', { pointerId: 1, clientX: 100, preventDefault() {} });
  hit.dispatch('pointermove', { pointerId: 1, clientX: 950 });
  hit.dispatch('pointerup', { pointerId: 1, clientX: 950 });

  assert.equal(elements.get('filterDate').value, '2026-09-19');
  assert.equal(elements.get('cutFilterDate').value, '2026-09-19');
});

test('live stream shows loading indicator while connecting and hides it on load', () => {
  const html = fs.readFileSync('index.html', 'utf8');
  assert.ok(html.includes('id="liveLoading"'));
  assert.ok(html.includes('id="cutLiveLoading"'));
  assert.ok(html.includes('.live-loading'));
  assert.ok(html.includes('.live-spinner'));

  const { context } = loadReplayScript();
  const loading = context.document.getElementById('liveLoading');
  const stream = context.document.getElementById('liveStream');

  context.setScreenLive('replay', true);
  assert.equal(loading.hidden, false);
  assert.equal(stream.hidden, true);

  assert.equal(typeof stream.onload, 'function');
  stream.onload();
  assert.equal(loading.hidden, true);
  assert.equal(stream.hidden, false);

  context.setScreenLive('replay', false);
  assert.equal(loading.hidden, true);
  assert.equal(stream.hidden, true);
  assert.equal(stream.onload, null);
});

test('seeking into gap automatically snaps to the nearest recorded segment', () => {
  const { context, elements } = loadReplayScript();
  vm.runInContext(`visibleVideos = [
    { name: 'cam1', started_at: '2026-09-20T10:00:00', end_at: '2026-09-20T10:10:00' },
    { name: 'cam2', started_at: '2026-09-20T10:30:00', end_at: '2026-09-20T10:40:00' }
  ]; currentVideo = null;`, context);

  // 10:12:00 is 2 mins after cam1 end, 18 mins before cam2 start -> snaps to cam1 end
  const p1 = context.getTimelineProgressForDate(new Date('2026-09-20T10:12:00'));
  context.seekTimelineProgress('replay', p1, true, true);
  const player = elements.get('videoPlayer');
  assert.equal(player.paused, false);
  assert.match(player.src, /at=2026-09-20T10%3A09%3A59/);
  assert.equal(vm.runInContext('currentVideo.name', context), 'cam1');

  // 10:28:00 is 18 mins after cam1 end, 2 mins before cam2 start -> snaps to cam2 start
  const p2 = context.getTimelineProgressForDate(new Date('2026-09-20T10:28:00'));
  context.seekTimelineProgress('replay', p2, true, true);
  assert.match(player.src, /at=2026-09-20T10%3A30%3A00/);
  assert.equal(vm.runInContext('currentVideo.name', context), 'cam2');

  // 08:00:00 is before all videos -> snaps to first video start (cam1 at 10:00:00)
  const p3 = context.getTimelineProgressForDate(new Date('2026-09-20T08:00:00'));
  context.seekTimelineProgress('replay', p3, true, true);
  assert.match(player.src, /at=2026-09-20T10%3A00%3A00/);
  assert.equal(vm.runInContext('currentVideo.name', context), 'cam1');

  // 12:00:00 is after all videos -> snaps to last video end (cam2 at 10:39:59)
  const p4 = context.getTimelineProgressForDate(new Date('2026-09-20T12:00:00'));
  context.seekTimelineProgress('replay', p4, true, true);
  assert.match(player.src, /at=2026-09-20T10%3A39%3A59/);
  assert.equal(vm.runInContext('currentVideo.name', context), 'cam2');
});

test('cut mode defaults to 30-minute range and updates labels and visual masks', () => {
  const html = fs.readFileSync('index.html', 'utf8');
  assert.ok(html.includes('id="cutMaskLeft"'));
  assert.ok(html.includes('id="cutMaskRight"'));
  assert.ok(html.includes('.cut-mask'));

  const { context, elements } = loadReplayScript();
  const startTarget = new Date('2026-09-20T10:15:00');
  context.configureClipRange(startTarget);

  const startMs = vm.runInContext('clipStartAt.getTime()', context);
  const endMs = vm.runInContext('clipEndAt.getTime()', context);
  assert.equal((endMs - startMs) / 1000, 30 * 60);
  assert.equal(elements.get('clipStartLabel').textContent, '10:15:00');
  assert.equal(elements.get('clipEndLabel').textContent, '10:45:00');
  assert.equal(elements.get('clipDurationLabel').textContent, '00:30:00');

  // Mask styles updated
  assert.ok(elements.get('cutMaskLeft').style.width.endsWith('%'));
  assert.ok(elements.get('cutMaskRight').style.width.endsWith('%'));
});

test('in cut mode, timeline cannot be dragged or sought outside the clip range bounds', () => {
  const { context, elements } = loadReplayScript();
  vm.runInContext(`visibleVideos = [
    { name: 'cam1', started_at: '2026-09-20T00:00:00', end_at: '2026-09-20T23:59:59' }
  ]; currentVideo = visibleVideos[0];
  clipStartAt = new Date('2026-09-20T10:00:00');
  clipEndAt = new Date('2026-09-20T10:30:00');
  updateClipVisual();`, context);

  const minProg = vm.runInContext('getTimelineProgressForDate(clipStartAt)', context);
  const maxProg = vm.runInContext('getTimelineProgressForDate(clipEndAt)', context);

  // Seeking before clipStartAt clamps to clipStartAt
  const beforeProg = context.getTimelineProgressForDate(new Date('2026-09-20T08:00:00'));
  context.seekTimelineProgress('cut', beforeProg, false, true);
  assert.equal(context.timelineStates.cut.progress, minProg);

  // Seeking after clipEndAt clamps to clipEndAt
  const afterProg = context.getTimelineProgressForDate(new Date('2026-09-20T12:00:00'));
  context.seekTimelineProgress('cut', afterProg, false, true);
  assert.equal(context.timelineStates.cut.progress, maxProg);

  // Wire cut timeline hit and test keyboard clamp
  context.wireTimeline('cutTimelineHit', true);
  const cutHit = context.document.getElementById('cutTimelineHit');

  // Pressing ArrowLeft while at minProg cannot move below minProg
  context.timelineStates.cut.progress = minProg;
  cutHit.dispatch('keydown', { key: 'ArrowLeft', shiftKey: false, preventDefault() {} });
  assert.equal(context.timelineStates.cut.progress, minProg);

  // Pressing ArrowRight while at maxProg cannot move above maxProg
  context.timelineStates.cut.progress = maxProg;
  cutHit.dispatch('keydown', { key: 'ArrowRight', shiftKey: false, preventDefault() {} });
  assert.equal(context.timelineStates.cut.progress, maxProg);
});
