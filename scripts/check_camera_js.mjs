#!/usr/bin/env node
// Behavioural check of the *browser's* camera frame gate and popup builder.
//
// Why this is JavaScript and not another Python test: everything
// tests/test_camera_layer.py can say about the browser's copy of the gate is
// text. It reads `p.hostname !== CAMERA_FRAME_HOST` out of the file and asserts
// the string is present, which pins the shape of what we shipped rather than
// the property we want. A rewrite that returned the URL untouched -- leaving the
// hostname comparison alive in a comment, or behind a `if (false)` -- would pass
// every assertion in that file while the last line of defence was gone. This
// script instead lifts the real functions out of the real file and runs
// hostile URLs through them.
//
// The functions come from `static/js/public_map.js` itself, not from a copy: a
// copy is a second thing to keep in sync, and the day it drifts is the day this
// check starts lying. Nothing is written anywhere -- the neutered variant in the
// witness below is a string, never the file on disk.
//
// No npm dependencies, so CI needs no install step, and node 18+ is enough.
//
//   node scripts/check_camera_js.mjs [path/to/public_map.js]
//
// Exit 0: every property held. Exit 1: at least one did not, or the check
// itself could not be trusted.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const JS_PATH = resolve(process.argv[2] || resolve(ROOT, 'static/js/public_map.js'));

const NEEDED = ['esc', 'safeUrl', 'cameraFrameUrl', 'cameraFrameHtml', 'cameraPopupHtml'];

const src = readFileSync(JS_PATH, 'utf8');

// Pull one function out of the source text.
//
// This is a brace counter, not a parser: a `}` inside a string literal or a
// regex would end the slice early. That failure mode is loud rather than quiet,
// because the concatenation below is handed to `new Function`, so a truncated
// function is a syntax error and the whole check refuses to run. It cannot
// return a wrong-but-valid answer without that. Anchoring on `^function` at
// column zero also means a mention of the name inside a comment cannot be
// mistaken for the definition.
// The text is a parameter rather than closed over, because the witness below
// loads a *different* text through this same path. Reading the module-level
// source here instead silently tested the real functions while claiming to test
// the neutered ones -- which is precisely what the witness caught the first
// time it ran.
function grab(text, name) {
  const at = text.search(new RegExp(`^function ${name}\\(`, 'm'));
  if (at < 0) throw new Error(`${name}() not found in ${JS_PATH}`);
  let depth = 0;
  for (let i = text.indexOf('{', at); i < text.length; i++) {
    if (text[i] === '{') depth++;
    else if (text[i] === '}' && --depth === 0) return text.slice(at, i + 1);
  }
  throw new Error(`unbalanced braces in ${name}()`);
}

function load(text) {
  const host = text.match(/^var CAMERA_FRAME_HOST = '[^']+';$/m);
  if (!host) throw new Error('CAMERA_FRAME_HOST is not declared where it should be');
  const body = [host[0], ...NEEDED.map((n) => grab(text, n))].join('\n');
  const fx = new Function(
    body + '\nreturn { esc, safeUrl, cameraFrameUrl, cameraFrameHtml, cameraPopupHtml };',
  )();
  // Extraction integrity: the host the functions close over must be the host the
  // file declares, read from the same text rather than hardcoded here. If the
  // slice had picked up a stale or shadowed declaration, this is where it shows.
  const declared = host[0].match(/'([^']+)'/)[1];
  if (!body.includes(`var CAMERA_FRAME_HOST = '${declared}';`)) {
    throw new Error('extracted CAMERA_FRAME_HOST does not match the declaration');
  }
  fx.CAMERA_FRAME_HOST = declared;
  return fx;
}

// ---------------------------------------------------------------------------
// The properties. Each one is a statement about behaviour, and each is written
// so that the obvious wrong implementation fails it.
// ---------------------------------------------------------------------------

// Everything that is not plainly the city's own published frame. The point of
// the list is breadth of *kind*, not volume: each entry is a different way the
// check could be defeated (scheme, host, suffix host, path shape, query,
// fragment, credentials, port, non-URL scheme, wrong type).
const HOSTILE_URLS = [
  ['plain http', 'http://cctv.austinmobility.io/image/674.jpg'],
  ['other host', 'https://evil.example/image/674.jpg'],
  ['suffix host', 'https://cctv.austinmobility.io.evil.example/1.jpg'],
  ['prefix host', 'https://evilcctv.austinmobility.io/image/1.jpg'],
  ['other path', 'https://cctv.austinmobility.io/frames/674.jpg'],
  ['path traversal', 'https://cctv.austinmobility.io/image/../674.jpg'],
  ['not a jpg', 'https://cctv.austinmobility.io/image/674.php'],
  ['query string', 'https://cctv.austinmobility.io/image/674.jpg?a=1'],
  ['fragment', 'https://cctv.austinmobility.io/image/674.jpg#a'],
  ['credentials', 'https://u:p@cctv.austinmobility.io/image/1.jpg'],
  ['odd port', 'https://cctv.austinmobility.io:8443/image/1.jpg'],
  ['javascript scheme', 'javascript:alert(1)'],
  ['data scheme', 'data:text/html,<script>alert(1)</script>'],
  ['protocol relative', '//cctv.austinmobility.io/image/674.jpg'],
  ['not a string', undefined],
  ['a number', 42],
  ['an object', { href: 'https://cctv.austinmobility.io/image/674.jpg' }],
  ['whitespace', '   '],
  ['bare host', 'https://cctv.austinmobility.io'],
];

function audit(fx, label) {
  let failures = 0;
  const check = (what, got, want) => {
    const ok = JSON.stringify(got) === JSON.stringify(want);
    if (!ok) failures++;
    console.log(`  ${ok ? 'ok  ' : 'FAIL'} [${label}] ${what}` +
      (ok ? '' : `\n         got:  ${JSON.stringify(got)}\n         want: ${JSON.stringify(want)}`));
  };

  console.log(`\n== [${label}] the gate keeps the city's own frame ==`);
  const good = 'https://cctv.austinmobility.io/image/674.jpg';
  check('accepts a real published frame', fx.cameraFrameUrl(good), good);

  console.log(`\n== [${label}] the gate rejects everything else ==`);
  for (const [what, bad] of HOSTILE_URLS) {
    check(`rejects ${what}`, fx.cameraFrameUrl(bad), '');
  }

  console.log(`\n== [${label}] popup markup: one image, lazily, safely ==`);
  const popup = fx.cameraPopupHtml({ id: '674', name: 'CESAR CHAVEZ ST / 35 SVRD', image: good }, '2026-10-02T16:33:29Z');
  check('carries the frame', popup.includes(`<img class="cam-frame" src="${good}"`), true);
  check('loads it lazily', popup.includes('loading="lazy"'), true);
  check('does not leak a referrer', popup.includes('referrerpolicy="no-referrer"'), true);
  check('opens the full size copy safely', popup.includes('target="_blank" rel="noopener noreferrer"'), true);
  check('exactly one image', (popup.match(/<img/g) || []).length, 1);
  check('does not claim a live stream', popup.includes('not a live video stream'), true);
  check('does not claim a surveyed position', popup.includes('approximate'), true);
  check('credits the frame host', popup.includes('cctv.austinmobility.io'), true);
  check('shows the snapshot date it was given', popup.includes('2026-10-02T16:33:29Z'), true);

  console.log(`\n== [${label}] a camera name cannot become markup ==`);
  const hostile = fx.cameraPopupHtml(
    { id: '9', name: '"><img src=x onerror=alert(1)>', image: 'https://cctv.austinmobility.io/image/9.jpg' },
    '',
  );
  check('still exactly one image', (hostile.match(/<img/g) || []).length, 1);
  check('the payload is escaped', hostile.includes('&lt;img src&#x3D;x onerror&#x3D;alert(1)&gt;'), true);
  check('no live handler reaches the markup', hostile.includes('onerror=alert(1)'), false);

  console.log(`\n== [${label}] a camera with no published frame ==`);
  const bare = fx.cameraPopupHtml({ id: '9', name: 'NO IMAGE HERE' }, '');
  check('renders no image at all', (bare.match(/<img/g) || []).length, 0);
  check('says the city publishes none', bare.includes('publishes no image'), true);

  // Same question asked of a frame the city does not publish. The popup is the
  // last place a bad snapshot value could become a request, so the gate has to
  // hold here too, not only in the fetcher.
  const foreign = fx.cameraPopupHtml(
    { id: '9', name: 'SOMEWHERE', image: 'https://evil.example/image/9.jpg' }, '',
  );
  check('a frame on another host renders no image', (foreign.match(/<img/g) || []).length, 0);
  check('and says the city publishes none', foreign.includes('publishes no image'), true);

  return failures;
}

// ---------------------------------------------------------------------------

let fx;
try {
  fx = load(src);
} catch (err) {
  console.error(`could not load the camera functions: ${err.message}`);
  process.exit(1);
}

const failures = audit(fx, 'real');

// -- witness -----------------------------------------------------------------
// A guard that has never been shown failing might have stopped guarding, so
// neuter the gate and require these same checks to notice. The replacement is
// applied to a string: an earlier witness in this repo rewrote the real source
// file, and when its assertion failed half way the restore in `finally` never
// ran and left a typo in the working tree -- the same route by which unrelated
// drift blocks a deploy.
const neuteredText = src.replace(
  /^function cameraFrameUrl\(v\) \{[\s\S]*?^\}/m,
  'function cameraFrameUrl(v) {\n  return safeUrl(v);\n}',
);
if (neuteredText === src) {
  console.error('\nWITNESS FAILED: could not neuter cameraFrameUrl, so nothing was proved.');
  process.exit(1);
}
const caught = audit(load(neuteredText), 'witness: gate neutered');
if (caught === 0) {
  console.error('\nWITNESS FAILED: a gate that accepts every URL passed these checks.');
  process.exit(1);
}
console.log(`\n== witness: the neutered gate was caught by ${caught} checks ==`);

console.log(`\nCAMERA_JS_CHECKS: ${failures ? failures + ' FAILED' : 'ok'}`);
process.exit(failures ? 1 : 0);