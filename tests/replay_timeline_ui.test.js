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
