#!/usr/bin/env node
// Prove a camera dot can actually be clicked, by clicking one.
//
// Why this exists as a script and not a screenshot review: 820 dots drew
// perfectly, the console was clean, the metrics were right, the screenshot passed
// review -- and no camera popup had ever opened, because the ADS-B heatmap
// plugin's canvas sat above the camera canvas in the overlay pane and ate every
// click. Nothing short of dispatching a real event finds that. See
// tests/test_camera_layer.py::TestCameraDotsAreActuallyClickable for the cheap
// structural guard that now sits in CI; this is the expensive version that
// exercises the shipped page, and it is the one that found the defect.
//
// It talks to Chrome over the DevTools Protocol directly, with no npm
// dependencies -- node 18+ has a global WebSocket, which is all CDP needs.
//
//   node scripts/map_interaction_check.mjs [url] [--out DIR] [--keep-open]
//
// Defaults to the public map. Point it at a local instance to test a change
// before it ships:
//
//   node scripts/map_interaction_check.mjs http://127.0.0.1:8080/
//
// Exit 0 every property held. Exit 1 a property failed. Exit 2 it could not run
// at all (no Chrome), which is deliberately distinct from passing: a check that
// cannot run must never be reported as one that did.

import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { existsSync } from 'node:fs';

const argv = process.argv.slice(2);
const flag = (name) => argv.includes(name);
const positional = argv.filter((a) => !a.startsWith('--'));
const URL_ = positional[0] || 'https://battlebuddy.news/';
const OUT = (() => {
  const i = argv.indexOf('--out');
  return i >= 0 && argv[i + 1] ? argv[i + 1] : '/tmp/bb-map-shots';
})();
const PORT = 9333;
const W = 1440, H = 900;
const LOAD_WAIT_MS = 8000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------------------
// Chrome
// ---------------------------------------------------------------------------

const CHROME_CANDIDATES = [
  process.env.CHROME_BIN,
  'google-chrome-stable', 'google-chrome', 'chromium', 'chromium-browser',
].filter(Boolean);

function findChrome() {
  for (const bin of CHROME_CANDIDATES) {
    if (bin.includes('/')) {
      if (existsSync(bin)) return bin;
      continue;
    }
    for (const dir of (process.env.PATH || '').split(':')) {
      const p = `${dir}/${bin}`;
      if (dir && existsSync(p)) return p;
    }
  }
  return null;
}

const chromeBin = findChrome();
if (!chromeBin) {
  console.error('No Chrome or Chromium found, so this check could not run.');
  console.error('Install one, or set CHROME_BIN to its path.');
  console.error('This is NOT a pass. Exiting 2 so it cannot be mistaken for one.');
  process.exit(2);
}

mkdirSync(OUT, { recursive: true });
console.log(`chrome:  ${chromeBin}`);
console.log(`url:     ${URL_}`);
console.log(`out:     ${OUT}`);

const chrome = spawn(chromeBin, [
  '--headless=new',
  `--remote-debugging-port=${PORT}`,
  `--window-size=${W},${H}`,
  '--user-data-dir=/tmp/cdp-bb-map-check',
  '--no-sandbox',
  '--disable-gpu',
  '--hide-scrollbars',
  'about:blank',
], { stdio: 'ignore' });

const bail = (code, msg) => {
  console.error(msg);
  try { chrome.kill(); } catch { /* already gone */ }
  process.exit(code);
};

let target = null;
for (let i = 0; i < 40 && !target; i++) {
  await sleep(500);
  try {
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    target = list.find((t) => t.type === 'page');
  } catch { /* not listening yet */ }
}
if (!target) bail(2, 'Chrome DevTools never came up. Exiting 2: did not run.');

const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res, rej) => {
  ws.onopen = res;
  ws.onerror = () => rej(new Error('could not attach to the DevTools socket'));
});

let nextId = 0;
const pending = new Map();
const pageExceptions = [];
const consoleErrors = [];

ws.onmessage = (m) => {
  const msg = JSON.parse(m.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    return;
  }
  if (msg.method === 'Runtime.exceptionThrown') {
    const d = msg.params?.exceptionDetails;
    pageExceptions.push(d?.exception?.description || d?.text || 'unknown exception');
  }
  if (msg.method === 'Runtime.consoleAPICalled' && msg.params?.type === 'error') {
    consoleErrors.push((msg.params.args || []).map((a) => a.value ?? a.description ?? '').join(' '));
  }
};

const send = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const id = ++nextId;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });

const evaluate = async (expression) => {
  const r = await send('Runtime.evaluate', {
    expression, returnByValue: true, awaitPromise: true,
  });
  if (r.exceptionDetails) {
    throw new Error(
      (r.exceptionDetails.exception?.description || r.exceptionDetails.text) +
      ' :: ' + expression.slice(0, 120),
    );
  }
  return r.result.value;
};

const shot = async (name) => {
  const r = await send('Page.captureScreenshot', { format: 'png' });
  const path = `${OUT}/${name}.png`;
  writeFileSync(path, Buffer.from(r.data, 'base64'));
  console.log(`  screenshot: ${path}`);
};

const click = async (x, y) => {
  for (const type of ['mousePressed', 'mouseReleased']) {
    await send('Input.dispatchMouseEvent', {
      type, x, y, button: 'left', clickCount: 1,
      buttons: type === 'mousePressed' ? 1 : 0,
    });
  }
};

// ---------------------------------------------------------------------------

await send('Page.enable');
await send('Runtime.enable');
await send('Emulation.setDeviceMetricsOverride', {
  width: W, height: H, deviceScaleFactor: 1, mobile: false,
});

console.log('navigating...');
await send('Page.navigate', { url: URL_ });
await sleep(LOAD_WAIT_MS);

let failures = 0;
const check = (what, ok, detail) => {
  if (!ok) failures++;
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${what}` + (detail ? `\n         ${detail}` : ''));
};

// -- 1. the layer is on the page at all ---------------------------------------

const state = await evaluate(`JSON.stringify({
  dots: typeof cameraDots !== 'undefined' && cameraDots ? cameraDots.length : 0,
  markers: document.querySelectorAll('.leaflet-marker-icon').length,
  url: location.href,
})`).then(JSON.parse);

console.log(`\n== the layer loaded ==\n  ${JSON.stringify(state)}`);
check('camera dots are on the page', state.dots > 0, `dots=${state.dots}`);
await shot('01-map');
if (state.dots === 0) {
  console.log('\ncannot go further without dots; that is the failure.');
  bail(1, 'no camera dots');
}

// -- 2. no canvas is painted over the camera canvas ---------------------------
// The defect in one sentence. Canvases stack, the top one takes the click, and
// the heatmap plugin draws a full-map canvas into overlayPane, which is exactly
// where the camera canvas used to be.

const stack = await evaluate(`(() => {
  const cam = cameraDots[0]._renderer._container;
  const camZ = parseInt(getComputedStyle(cam).zIndex, 10) || 0;
  const camPane = cam.closest('.leaflet-pane');
  const camPaneZ = parseInt(getComputedStyle(camPane).zIndex, 10) || 0;
  const r = cam.getBoundingClientRect();
  const above = [];
  for (const c of document.querySelectorAll('canvas')) {
    if (c === cam) continue;
    const cr = c.getBoundingClientRect();
    const overlaps = cr.width > 0 && cr.height > 0 &&
      cr.left < r.right && cr.right > r.left && cr.top < r.bottom && cr.bottom > r.top;
    if (!overlaps) continue;
    const pane = c.closest('.leaflet-pane');
    const paneZ = pane ? (parseInt(getComputedStyle(pane).zIndex, 10) || 0) : 0;
    const ownZ = parseInt(getComputedStyle(c).zIndex, 10) || 0;
    const effective = paneZ + ownZ;
    if (effective >= camPaneZ + camZ) {
      above.push({ cls: c.className || '(no class)', pane: pane ? pane.className : null, effective });
    }
  }
  return { camPane: camPane.className, camPaneZ, camZ, above };
})()`);

console.log(`\n== nothing paints over the camera canvas ==\n  ${JSON.stringify(stack)}`);
check(
  'no overlapping canvas stacks above the camera layer',
  stack.above.length === 0,
  stack.above.length ? `above it: ${JSON.stringify(stack.above)}` : '',
);

// -- 3. the topmost element over a real dot is the camera canvas -------------
// Reported per dot rather than asserted on one, because a dot genuinely under an
// incident pin is not a defect -- markerPane is meant to be on top. Zero
// camera-topmost dots across the map is the failure.

const hit = await evaluate(`(() => {
  const cam = cameraDots[0]._renderer._container;
  const m = cameraDots[0]._map || (cameraDots[0]._renderer && cameraDots[0]._renderer._map);
  if (!m) return { err: 'no map reference on the camera layer' };
  const r = m.getContainer().getBoundingClientRect();
  const step = Math.max(1, Math.floor(cameraDots.length / 60));
  let sampled = 0, onCanvas = 0, onMarker = 0, other = [];
  let pick = null;
  for (let i = 0; i < cameraDots.length; i += step) {
    const d = cameraDots[i];
    const p = m.latLngToContainerPoint(d.getLatLng());
    const x = p.x + r.left, y = p.y + r.top;
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) continue;
    const el = document.elementFromPoint(x, y);
    if (!el) continue;
    sampled++;
    if (el === cam) { onCanvas++; if (!pick) pick = { x, y, id: d.getLatLng() }; }
    else if (el.classList && el.classList.contains('leaflet-marker-icon')) onMarker++;
    else if (other.length < 4) other.push(el.tagName + '.' + (el.className || ''));
  }
  return { sampled, onCanvas, onMarker, other, pick };
})()`);

console.log(`\n== hit testing real dots ==\n  ${JSON.stringify(hit)}`);
check('the map was sampled', hit.sampled > 0, `sampled=${hit.sampled}`);
check(
  'camera dots are the topmost element at their own pixel',
  hit.onCanvas > 0,
  `onCanvas=${hit.onCanvas} onMarker=${hit.onMarker} other=${JSON.stringify(hit.other)}`,
);

// -- 4. a real dispatched click opens the popup -------------------------------

let popupText = '';
if (hit.pick) {
  // A dot's hit radius is a few pixels and a pin may sit within it, so try a few
  // points around the chosen one before calling it a miss.
  const offsets = [[0, 0], [3, 0], [-3, 0], [0, 3], [0, -3], [5, 0], [-5, 0], [0, 5], [0, -5]];
  for (const [ox, oy] of offsets) {
    await click(hit.pick.x + ox, hit.pick.y + oy);
    await sleep(300);
    popupText = await evaluate(
      `(document.querySelector('.leaflet-popup-content')||{}).textContent || ''`,
    );
    if (popupText.includes('City traffic camera')) break;
    await evaluate(`document.querySelector('.leaflet-popup-close-button')?.click()`);
    popupText = '';
  }
}

console.log(`\n== a real click ==\n  popup: ${JSON.stringify(popupText.slice(0, 120))}`);
check('a dispatched click opens the camera popup', popupText.includes('City traffic camera'));
await shot('02-after-click');

if (!popupText) {
  console.log('\nthe click did nothing. That is the failure this script exists for.');
  console.log('If nothing is on top of the camera canvas either, the dot may simply be');
  console.log('off screen at this viewport -- rerun against a taller window.');
  bail(1, 'click did not open a camera popup');
}

// -- 5. the popup is honest and the frame is real ----------------------------

check('the popup says the frame is not a live stream', popupText.includes('not a live video stream'));
check('it says the position is approximate', popupText.includes('approximate'));

const frame = await evaluate(`(async () => {
  const el = document.querySelector('img.cam-frame');
  if (!el) return { present: false };
  for (let i = 0; i < 60; i++) {
    if (el.complete && el.naturalWidth > 0) break;
    await new Promise(r => setTimeout(r, 500));
  }
  return {
    present: true,
    w: el.naturalWidth, h: el.naturalHeight,
    src: el.currentSrc || el.src,
    lazy: el.getAttribute('loading'),
    referrer: el.getAttribute('referrerpolicy'),
  };
})()`);

console.log(`\n== the city's frame ==\n  ${JSON.stringify(frame)}`);
check('the popup contains a frame element', frame.present);
check('the frame came from the city host', String(frame.src || '').startsWith('https://cctv.austinmobility.io/image/'));
check('the frame actually decoded', frame.w > 0 && frame.h > 0, `${frame.w}x${frame.h}`);
check('the frame is loaded lazily', frame.lazy === 'lazy');
await sleep(500);
await shot('03-camera-popup');

// -- 6. nothing threw ---------------------------------------------------------

console.log(`\n== page health ==\n  exceptions: ${pageExceptions.length}  console errors: ${consoleErrors.length}`);
if (pageExceptions.length) pageExceptions.forEach((e) => console.log(`    ! ${e.slice(0, 200)}`));
if (consoleErrors.length) consoleErrors.slice(0, 5).forEach((e) => console.log(`    ~ ${e.slice(0, 200)}`));
// Only uncaught exceptions fail the run. Console errors are reported and not
// fatal: the basemap is a third party, and a tile that 404s would otherwise
// fail a check about our own layer.
check('no uncaught exception on the page', pageExceptions.length === 0);

ws.close();
if (!flag('--keep-open')) chrome.kill();

console.log(`\nMAP_INTERACTION_CHECK: ${failures ? failures + ' FAILED' : 'ok'}`);
process.exit(failures ? 1 : 0);