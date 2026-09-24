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
    .replaceAll("{{ 'true' if license_active else 'false' }}", 'true')
    .replaceAll('{{ license_key }}', 'TEST-KEY')
    .replaceAll("{{ 'true' if has_nvr else 'false' }}", 'false')
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

test('preset playback rate buttons (0.5X, 1X, 2X, 4X) sit with section headers and zoom controls', () => {
  const html = fs.readFileSync('index.html', 'utf8');
  const { context } = loadReplayScript();
  ['0.5', '1', '2', '4'].forEach(rate => {
    assert.equal((html.match(new RegExp(`data-timeline-context="replay" data-rate="${rate}"`, 'g')) || []).length, 1);
    assert.equal((html.match(new RegExp(`data-timeline-context="cut" data-rate="${rate}"`, 'g')) || []).length, 1);
  });
  assert.ok(html.includes('Tốc độ phát'));
  assert.ok(html.includes('Thu phóng'));
  assert.ok(html.includes('id="timelineZoomOut"'));
  assert.ok(html.includes('id="cutTimelineZoomOut"'));
  assert.ok(Math.abs(context.DEFAULT_TIMELINE_ZOOM - 24) < 0.1);
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

test('preset playback rate buttons switch playbackRate directly and stopPlayback resets to 1X', () => {
  const { context } = loadReplayScript();
  vm.runInContext("currentVideo={started_at:'2026-09-20T00:00:00',end_at:'2026-09-20T00:10:00'}", context);
  const player = context.document.getElementById('videoPlayer');

  const btn05 = makeElement('btn05'); btn05.dataset = { rate: '0.5' };
  const btn2 = makeElement('btn2'); btn2.dataset = { rate: '2' };
  const btn4 = makeElement('btn4'); btn4.dataset = { rate: '4' };

  context.setPlaybackRate('replay', 0.5, btn05);
  assert.equal(player.playbackRate, 0.5);
  assert.ok(btn05.classList.contains('is-active'));

  context.setPlaybackRate('replay', 2, btn2);
  assert.equal(player.playbackRate, 2);
  assert.ok(btn2.classList.contains('is-active'));

  context.setPlaybackRate('replay', 4, btn4);
  assert.equal(player.playbackRate, 4);
  assert.ok(btn4.classList.contains('is-active'));

  context.stopPlayback(player, 'replay');
  assert.equal(player.playbackRate, 1);
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
  assert.equal(children[0].textContent, '00:00');
  assert.equal(children[23].textContent, '23:00');
  assert.equal(children[24].textContent, '24:0000:00');
  assert.equal(children[25].textContent, '01:00');
  assert.equal(children[26].textContent, '02:00');
  assert.equal(children[48].textContent, '24:00');
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

test('cut mode formats clip times cleanly as HH:mm:ss without (Hôm trước) indicator', () => {
  const { context, elements } = loadReplayScript();
  elements.get('filterDate').value = '2026-09-20';
  vm.runInContext(`
    clipStartAt = new Date('2026-09-19T23:30:00');
    clipEndAt = new Date('2026-09-20T00:00:00');
    updateClipVisual();
  `, context);
  assert.equal(elements.get('clipStartLabel').textContent, '23:30:00');
  assert.equal(elements.get('clipEndLabel').textContent, '00:00:00');
});

test('live button icon uses clean radio broadcast waves SVG path', () => {
  const html = fs.readFileSync('index.html', 'utf8');
  assert.ok(html.includes('d="M4.93 19.07a10 10 0 0 1 0-14.14M19.07 4.93a10 10 0 0 1 0 14.14M7.76 16.24a6 6 0 0 1 0-8.48M16.24 7.76a6 6 0 0 1 0 8.48"'));
  assert.doesNotMatch(html, /M7\.76 4\.76a10/);
});

test('showCut triggers requestAnimationFrame rendering for cutFilmstrip and cutRuler', () => {
  const { context, elements } = loadReplayScript();
  let rafCallback = null;
  context.requestAnimationFrame = cb => { rafCallback = cb; return 1; };
  vm.runInContext(`visibleVideos = [
    { name: 'cam1', started_at: '2026-09-20T00:00:00', end_at: '2026-09-20T23:59:59' }
  ]; currentVideo = visibleVideos[0];`, context);

  context.showCut();
  assert.equal(elements.get('cutScreen').hidden, false);
  assert.ok(typeof rafCallback === 'function');
  rafCallback();
  assert.ok(elements.get('cutRuler').children.length > 0);
});

test('merge button embeds cutting progress bar and resets cleanly', () => {
  const html = fs.readFileSync('index.html', 'utf8');
  assert.ok(html.includes('id="mergeButtonFill"'));
  assert.ok(html.includes('id="mergeButtonText"'));
  assert.ok(html.includes('.merge-button-fill'));
  assert.ok(html.includes('.action.cut-primary.is-processing'));

  const { context, elements } = loadReplayScript();
  const btn = makeElement('mergeButton'); elements.set('mergeButton', btn);
  const fill = makeElement('mergeButtonFill'); elements.set('mergeButtonFill', fill);
  const text = makeElement('mergeButtonText'); elements.set('mergeButtonText', text);

  context.setMergeProgress(45, 'Đang xử lý... 45%');
  assert.equal(btn.classList.contains('is-processing'), true);
  assert.equal(fill.style.width, '45%');
  assert.equal(text.textContent, 'Đang xử lý... 45%');

  context.resetMergeButton();
  assert.equal(btn.disabled, false);
  assert.equal(btn.classList.contains('is-processing'), false);
  assert.equal(fill.style.width, '0%');
  assert.equal(text.textContent, 'Cắt và tải về');
});

test('getMaxClipEndTime clamps clip range to current time when today is selected', () => {
  const { context, elements } = loadReplayScript();
  const todayStr = context.fmtDateInput(new Date());
  elements.get('filterDate').value = todayStr;

  const day = context.getTimelineDayBounds();
  const maxEndMs = context.getMaxClipEndTime(day);
  assert.ok(maxEndMs <= Date.now() + 50);

  // If focus is set to now, clipEndAt does not exceed maxEndMs
  context.configureClipRange(new Date(Date.now() + 100000));
  const endMs = vm.runInContext('clipEndAt.getTime()', context);
  assert.ok(endMs <= maxEndMs);
});

test('wireTimeline supports card-level scrubbing and pointer events', () => {
  const { context } = loadReplayScript();
  vm.runInContext(`visibleVideos = [
    { name: 'cam1', started_at: '2026-09-20T00:00:00', end_at: '2026-09-20T23:59:59' }
  ]; currentVideo = visibleVideos[0];`, context);
  const card = context.document.getElementById('timelineCard');
  const hit = context.document.getElementById('timelineHit');
  const viewport = context.document.getElementById('timelineViewport');
  const track = context.document.getElementById('timelineTrack');
  viewport.getBoundingClientRect = () => ({ left: 0, width: 1000, right: 1000, top: 0, height: 100 });
  context.wireTimeline('timelineHit', false);

  context.timelineStates.replay.progress = 0.6;
  context.timelineStates.replay.zoom = 1;

  // Trigger drag via timelineCard
  card.dispatch('pointerdown', { pointerId: 1, clientX: 500, preventDefault() {} });
  card.dispatch('pointermove', { pointerId: 1, clientX: 550, preventDefault() {} });
  card.dispatch('pointerup', { pointerId: 1 });

  // Progress updated by drag within recorded video
  assert.notEqual(context.timelineStates.replay.progress, 0.6);
});

test('doneScreen integrates video preview player and dual action buttons for iOS and desktop', () => {
  const { context } = loadReplayScript();
  const doneVideo = context.document.getElementById('doneVideoPreview');
  const openBtn = context.document.getElementById('openVideoBtn');
  const dlBtn = context.document.getElementById('mergedDownloadBtn');
  const guideBox = context.document.getElementById('iosSaveGuide');

  // Verify elements exist in mock DOM
  assert.ok(doneVideo);
  assert.ok(openBtn);
  assert.ok(dlBtn);
  assert.ok(guideBox);

  // Simulate completion
  const filename = 'cam1_2026-09-23_test.mp4';
  const inlineUrl = `/video/${encodeURIComponent(filename)}`;
  const dlUrl = `/download/${encodeURIComponent(filename)}`;

  doneVideo.src = inlineUrl;
  openBtn.href = inlineUrl;
  context.setAnchor(dlBtn, dlUrl, filename);

  assert.equal(doneVideo.src, inlineUrl);
  assert.equal(openBtn.href, inlineUrl);
  assert.equal(dlBtn.href, dlUrl);

  // Transitioning to replay resets preview video
  context.showReplay();
  assert.equal(context.document.getElementById('doneScreen').hidden, true);
  assert.equal(doneVideo.src, '');
});

test('instant cut screen transition and dual-stage progress bar on doneScreen', () => {
  const { context, elements } = loadReplayScript();

  // 1. showDone("cutting")
  context.showDone("cutting");
  assert.equal(elements.get('doneScreen').hidden, false);
  assert.equal(elements.get('cutScreen').hidden, true);
  assert.equal(elements.get('replayScreen').hidden, true);
  assert.equal(elements.get('doneProcessingIcon').style.display, '');
  assert.equal(elements.get('doneSuccessIcon').style.display, 'none');
  assert.equal(elements.get('doneProgressWrap').style.display, 'block');
  assert.equal(elements.get('doneVideoWrap').style.display, 'none');
  assert.equal(elements.get('doneActions').style.display, 'none');
  assert.ok(elements.get('doneTitle').textContent.includes('Đang xử lý'));

  // 2. showDone("downloading")
  context.showDone("downloading");
  assert.equal(elements.get('doneProgressWrap').style.display, 'block');
  assert.equal(elements.get('doneVideoWrap').style.display, 'none');
  assert.ok(elements.get('doneTitle').textContent.includes('Đang nạp video'));
  assert.equal(elements.get('doneProgressDetail').textContent, 'Giai đoạn 2/2: Nạp vào máy');

  // 3. showDone("ready")
  context.showDone("ready");
  assert.equal(elements.get('doneProcessingIcon').style.display, 'none');
  assert.equal(elements.get('doneSuccessIcon').style.display, '');
  assert.equal(elements.get('doneProgressWrap').style.display, 'none');
  assert.equal(elements.get('doneVideoWrap').style.display, 'block');
  assert.equal(elements.get('doneActions').style.display, 'flex');

  // 4. showDone("error", "Lỗi test")
  context.showDone("error", "Lỗi test cắt video");
  assert.equal(elements.get('doneProgressWrap').style.display, 'none');
  assert.equal(elements.get('doneVideoWrap').style.display, 'none');
  assert.equal(elements.get('doneActions').style.display, 'flex');
  assert.equal(elements.get('doneError').style.display, 'block');
  assert.equal(elements.get('doneError').textContent, 'Lỗi test cắt video');
});

test('wireTimeline supports multi-touch pinch-to-zoom gesture', () => {
  const { context } = loadReplayScript();
  vm.runInContext(`visibleVideos = [
    { name: 'cam1', started_at: '2026-09-20T00:00:00', end_at: '2026-09-20T23:59:59' }
  ]; currentVideo = visibleVideos[0];`, context);
  const card = context.document.getElementById('timelineCard');
  const viewport = context.document.getElementById('timelineViewport');
  viewport.getBoundingClientRect = () => ({ left: 0, width: 1000, right: 1000, top: 0, height: 100 });
  context.wireTimeline('timelineHit', false);

  context.timelineStates.replay.zoom = 10;

  // Touch 1 down at x=100, y=50
  card.dispatch('pointerdown', { pointerId: 10, clientX: 100, clientY: 50, preventDefault() {} });
  // Touch 2 down at x=200, y=50 -> initial dist = 100
  card.dispatch('pointerdown', { pointerId: 11, clientX: 200, clientY: 50, preventDefault() {} });

  // Move touch 2 to x=300 -> new dist = 200 (ratio = 2.0)
  card.dispatch('pointermove', { pointerId: 11, clientX: 300, clientY: 50, preventDefault() {} });

  // Zoom should have doubled: 10 * 2 = 20
  assert.equal(context.timelineStates.replay.zoom, 20);

  // Release touches
  card.dispatch('pointerup', { pointerId: 10 });
  card.dispatch('pointerup', { pointerId: 11 });
});

