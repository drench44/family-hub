// The seasonal scenes are generated art (scripts/gen-season-art.mjs). This
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
const SCENE = /^[a-z]+-[a-z]+-(day|eve)\.svg$/;
// CRLF-normalized: a Windows checkout with autocrlf must not fail these
const read = (p) => readFileSync(p, 'utf8').replace(/\r\n/g, '\n');
const scenes = () => readdirSync(committed).filter((f) => SCENE.test(f)).sort();

test('the committed scenes are exactly what the generator produces', () => {
  const out = mkdtempSync(join(tmpdir(), 'season-art-'));
  try {
    const run = spawnSync(process.execPath, [join(repo, 'scripts', 'gen-season-art.mjs')], {
      env: { ...process.env, SEASON_ART_OUT: out }, encoding: 'utf8',
    });
    assert.equal(run.status, 0, `generator failed: ${run.stderr}`);
    const made = readdirSync(out).sort();
    assert.ok(made.length >= 6, 'every look has a day and an evening scene');
    assert.deepEqual(scenes(), made, 'no stale or missing scene files');
    for (const f of made) {
      assert.equal(read(join(committed, f)), read(join(out, f)),
        `${f} differs from the generator's output (re-run scripts/gen-season-art.mjs)`);
    }
  } finally {
    rmSync(out, { recursive: true, force: true });
  }
});

test('every look ships both a day and an evening scene', () => {
  const files = scenes();
  const looks = new Set(files.map((f) => f.replace(/-(day|eve)\.svg$/, '')));
  for (const look of looks) {
    assert.ok(files.includes(`${look}-day.svg`) && files.includes(`${look}-eve.svg`), `${look} has both variants`);
  }
});

test('each scene covers the screen from the bottom and resolves all its own references', () => {
  for (const f of scenes()) {
    const svg = read(join(committed, f));
    assert.match(svg, /viewBox="0 0 1920 1080" preserveAspectRatio="xMidYMax slice"/, `${f} is the 16:9 wall canvas`);
    const ids = [...svg.matchAll(/ id="([^"]+)"/g)].map((m) => m[1]);
    assert.equal(new Set(ids).size, ids.length, `${f} has no duplicate ids`);
    for (const m of svg.matchAll(/url\(#([^)]+)\)|href="#([^"]+)"/g)) {
      const ref = m[1] || m[2];
      assert.ok(ids.includes(ref), `${f} references #${ref}, which it never defines`);
    }
    assert.ok(!/NaN|undefined/.test(svg), `${f} has no NaN/undefined coordinates or colours`);
    assert.ok(svg.length < 260 * 1024, `${f} stays light enough for a phone (${Math.round(svg.length / 1024)} KB)`);
  }
});
