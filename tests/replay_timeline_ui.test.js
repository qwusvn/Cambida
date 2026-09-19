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
    currentTime: 0,
    duration: 60,
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

function loadReplayScript() {
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
    .replaceAll('{{ camera_mode }}', 'nvr')
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

test('video controls reveal on interaction and auto-hide when idle', () => {
  const { context, elements, getTimer } = loadReplayScript();
  elements.set('playerFrame', makeElement('playerFrame'));
  const frame = elements.get('playerFrame');
  elements.get('pauseButton');
  elements.get('pauseIcon');
  elements.get('playerTime');
  elements.get('overlayTimestamp');
  elements.get('fullscreenButton');

  context.wirePlayer(
    'videoPlayer',
    'pauseButton',
    'pauseIcon',
    'playerTime',
    'overlayTimestamp',
    'playerFrame',
    'fullscreenButton',
    'replay',
  );
  const player = elements.get('videoPlayer');
  assert.equal(frame.classList.contains('is-controls-visible'), false);

  frame.dispatch('pointerdown', { pointerType: 'mouse' });
  assert.equal(frame.classList.contains('is-controls-visible'), true);
  assert.equal(getTimer()?.delay, 2400);

  getTimer().callback();
  assert.equal(frame.classList.contains('is-controls-visible'), false);
  assert.equal(player.paused, true);
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
