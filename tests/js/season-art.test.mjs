// The seasonal ridges are generated art (scripts/gen-season-art.mjs). This
// regenerates them into a temp dir and compares byte for byte with the
// committed files, so a hand edit to an SVG, or a generator change committed
// without its output, fails here instead of drifting silently.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const committed = join(repo, 'src', 'family_hub', 'web', 'static', 'seasons');

test('the committed ridge SVGs are exactly what the generator produces', () => {
  const out = mkdtempSync(join(tmpdir(), 'season-art-'));
  try {
    const run = spawnSync(process.execPath, [join(repo, 'scripts', 'gen-season-art.mjs')], {
      env: { ...process.env, SEASON_ART_OUT: out }, encoding: 'utf8',
    });
    assert.equal(run.status, 0, `generator failed: ${run.stderr}`);
    const made = readdirSync(out).sort();
    assert.ok(made.length >= 9, 'every look has its ridges');
    const onDisk = readdirSync(committed).filter((f) => /-ridge-\d+\.svg$/.test(f)).sort();
    assert.deepEqual(onDisk, made, 'no stale or missing ridge files');
    // CRLF-normalized: a Windows checkout with autocrlf must not fail this
    const read = (p) => readFileSync(p, 'utf8').replace(/\r\n/g, '\n');
    for (const f of made) {
      assert.equal(read(join(committed, f)), read(join(out, f)),
        `${f} differs from the generator's output (re-run scripts/gen-season-art.mjs)`);
    }
  } finally {
    rmSync(out, { recursive: true, force: true });
  }
});

test('every generated ridge is one closed path in a stretchable viewBox', () => {
  for (const f of readdirSync(committed).filter((n) => /-ridge-\d+\.svg$/.test(n))) {
    const svg = readFileSync(join(committed, f), 'utf8').replace(/\r\n/g, '\n');
    assert.match(svg, /preserveAspectRatio="none"/, `${f} must stretch to its layer`);
    assert.equal((svg.match(/<path /g) || []).length, 1, `${f} is a single mask path`);
    assert.match(svg, /Z"\/><\/svg>\n$/, `${f} closes its path`);
  }
});

test('every ridge tiles seamlessly: the land line ends at the height it starts', () => {
  // mask-repeat: repeat-x shows a step at every tile edge otherwise; the
  // byte-for-byte test would happily lock in a generator that broke this
  for (const f of readdirSync(committed).filter((n) => /-ridge-\d+\.svg$/.test(n))) {
    const d = readFileSync(join(committed, f), 'utf8').match(/ d="([^"]+)"/)[1];
    const land = d.slice(0, d.indexOf(' Z') + 2);   // the hill; trees follow as more subpaths
    const start = Number(land.match(/^M0 600 L0 (-?\d+)/)[1]);
    const end = Number(land.match(/L2400 (-?\d+) L2400 600 Z$/)[1]);
    assert.ok(Math.abs(start - end) <= 1, `${f} starts at y=${start} but ends at y=${end}`);
    // trees wrapped past an edge stay within one tile either side
    for (const x of d.matchAll(/[ML](-?\d+) /g)) {
      assert.ok(Number(x[1]) >= -2400 && Number(x[1]) <= 4800, `${f} has a stray x=${x[1]}`);
    }
  }
});
