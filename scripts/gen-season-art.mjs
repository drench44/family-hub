#!/usr/bin/env node
/* Generates the landscape silhouettes behind the seasonal looks.
 *
 *   node scripts/gen-season-art.mjs
 *
 * Writes src/family_hub/web/static/seasons/*-ridge-*.svg. Each file is ONE
 * black shape used as a CSS mask: the colour, gradient and haze all come from
 * the look's tokens in styles.css, so one shape serves the light and dark
 * flavour of a look. Every ridge is PERIODIC across its tile (integer-period
 * sines, trees wrapped past both edges), so `mask-repeat: repeat-x` tiles it
 * with no seam at any screen width.
 *
 * Deterministic (seeded), so a re-run reproduces the committed files byte for
 * byte. Change a seed or a parameter here, re-run, and commit both.
 */
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// SEASON_ART_OUT redirects the output (the test suite regenerates into a temp
// dir and compares against the committed files).
const OUT = process.env.SEASON_ART_OUT || join(dirname(fileURLToPath(import.meta.url)),
  "..", "src", "family_hub", "web", "static", "seasons");

const W = 2400;          // tile width (the hills repeat every W)
const H = 600;           // tile height
const STEP = 6;          // x resolution of the ridge line

function rng(seed) {     // mulberry32: small, seedable, good enough for art
  return function () {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/* A smooth, seamless ridge line: `base` is the mean height from the top (0..1
   of H); each wave is [periodsPerTile, amplitude (0..1 of H)]. Integer periods
   make y(0) === y(W), which is what makes repeat-x seamless. */
function ridgeLine(seed, base, waves) {
  const r = rng(seed);
  const phases = waves.map(() => r() * Math.PI * 2);
  return (x) => {
    let y = base;
    waves.forEach(([k, a], i) => { y += a * Math.sin((2 * Math.PI * k * x) / W + phases[i]); });
    return y * H;
  };
}

// whole pixels: on a 2400-wide tile, tenths are invisible and cost ~30% size
const f = (n) => Math.round(n);

function hillPath(line) {
  let d = `M0 ${H} L0 ${f(line(0))}`;
  for (let x = STEP; x <= W; x += STEP) d += ` L${x} ${f(line(x))}`;
  return d + ` L${W} ${H} Z`;
}

/* One spruce: a narrow spire over stacked, slightly drooping tiers. Drawn as a
   closed polygon standing on (x, y). */
function spruce(x, y, h, r) {
  const tiers = 4 + Math.floor(r() * 3);
  const half = h * (0.2 + r() * 0.06);
  const top = y - h;
  const pts = [[x, top]];
  const right = [];
  for (let t = 1; t <= tiers; t++) {
    const ty = top + (h * 0.92 * t) / tiers;
    const w = (half * t) / tiers;
    right.push([x + w, ty], [x + w * 0.42, ty - (h / tiers) * 0.28]);
  }
  right.pop();                                   // the last notch sits under the base
  pts.push(...right, [x + h * 0.03, y], [x - h * 0.03, y]);
  const left = right.map(([px, py]) => [2 * x - px, py]).reverse();
  pts.push(...left);
  return "M" + pts.map(([px, py]) => `${f(px)} ${f(py)}`).join(" L") + " Z";
}

/* A tree line standing on a hill: trees every `gap` px (jittered), heights in
   [hMin, hMax] (0..1 of H). Trees that straddle a tile edge are drawn on both
   sides, so the repeat stays seamless. */
function pinesPath(seed, line, gap, hMin, hMax) {
  const r = rng(seed);
  let d = hillPath(line);
  for (let x = 0; x < W; x += gap * (0.55 + r() * 0.9)) {
    const h = (hMin + r() * (hMax - hMin)) * H;
    const y = line(x) + h * 0.06;                // sink the trunk into the slope
    const shape = rng(Math.floor(x * 7919) + seed);
    d += " " + spruce(x, y, h, shape);
    if (x < h * 0.4) d += " " + spruce(x + W, y, h, rng(Math.floor(x * 7919) + seed));
    if (x > W - h * 0.4) d += " " + spruce(x - W, y, h, rng(Math.floor(x * 7919) + seed));
  }
  return d;
}

const svg = (d) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`
  + `<path d="${d}"/></svg>\n`;

const ART = {
  // Harvest: long, low swells of farmland, gentlest at the back.
  "harvest-ridge-1": hillPath(ridgeLine(11, 0.36, [[1, 0.07], [2, 0.05], [5, 0.012]])),
  "harvest-ridge-2": hillPath(ridgeLine(23, 0.42, [[1, 0.09], [3, 0.05], [7, 0.01]])),
  "harvest-ridge-3": hillPath(ridgeLine(37, 0.46, [[2, 0.1], [3, 0.045], [8, 0.008]])),
  "harvest-ridge-4": hillPath(ridgeLine(53, 0.5, [[1, 0.12], [2, 0.07], [5, 0.015]])),
  // Maple: two soft, low slopes so the leaves own the scene.
  "maple-ridge-1": hillPath(ridgeLine(61, 0.5, [[1, 0.1], [2, 0.05]])),
  "maple-ridge-2": hillPath(ridgeLine(67, 0.58, [[1, 0.12], [3, 0.03]])),
  // Woodland: layered spruce ridges receding into mist.
  "woodland-ridge-1": pinesPath(71, ridgeLine(71, 0.5, [[1, 0.08], [3, 0.04]]), 16, 0.07, 0.13),
  "woodland-ridge-2": pinesPath(79, ridgeLine(79, 0.56, [[2, 0.07], [5, 0.02]]), 24, 0.12, 0.22),
  "woodland-ridge-3": pinesPath(83, ridgeLine(83, 0.62, [[1, 0.06], [4, 0.03]]), 38, 0.22, 0.4),
};

mkdirSync(OUT, { recursive: true });
for (const [name, d] of Object.entries(ART)) {
  writeFileSync(join(OUT, `${name}.svg`), svg(d));
  if (!process.env.SEASON_ART_OUT) console.log(`${name}.svg  ${(svg(d).length / 1024).toFixed(1)} KB`);
}
