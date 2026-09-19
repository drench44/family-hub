#!/usr/bin/env node
/* Draws the seasonal look scenes.
 *
 *   node scripts/gen-season-art.mjs
 *
 * Writes src/family_hub/web/static/seasons/<look>-day.svg and -eve.svg: one
 * layered illustration per look, in a daytime palette (Light / Soft themes)
 * and an evening palette (Blue / Grey / Black). Both variants share every
 * shape; only the palette differs, so they always match.
 *
 * Each season offers the same spectrum of styles, so a family can pick how
 * much illustration they want on the wall:
 *   Minimal  - smooth gradient hills, very little detail (Apple-wallpaper calm)
 *   Scenic   - a painterly landscape with atmospheric depth
 *   Playful  - a storybook scene with props (a barn, pumpkins)
 *
 * Shapes are organic on purpose: canopies are noisy closed curves, spruces are
 * tiered silhouettes with uneven drooping branches, mountains are fractal
 * ridgelines. Circles and triangles read as clip-art.
 *
 * The canvas is 1920x1080 (the wall) and the CSS shows it with
 * `background-size: cover` anchored bottom-centre, so a phone in portrait sees
 * the middle ~500px of it. Things worth seeing therefore sit in the top strip
 * (the wall's top bar is open sky), the lower-left (under the chores column)
 * and the bottom middle (under the cameras, and on a phone).
 *
 * Deterministic (seeded): a re-run reproduces the committed files byte for
 * byte, and tests/js/season-art.test.mjs checks exactly that. Change a
 * parameter here, re-run, and commit both.
 */
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// SEASON_ART_OUT redirects the output (the test suite regenerates into a temp
// dir and compares against the committed files).
const OUT = process.env.SEASON_ART_OUT || join(dirname(fileURLToPath(import.meta.url)),
  "..", "src", "family_hub", "web", "static", "seasons");

const W = 1920;
const H = 1080;

function rng(seed) {     // mulberry32: small, seedable, good enough for art
  return function () {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const n = (v) => Math.round(v * 10) / 10;
const pick = (r, list) => list[Math.floor(r() * list.length)];

/* ----------------------------------------------------------- path helpers */

// Catmull-Rom through the points, as cubic Beziers: smooth organic outlines.
function smooth(pts, closed) {
  const P = (i) => pts[closed ? (i + pts.length) % pts.length : Math.max(0, Math.min(pts.length - 1, i))];
  const last = closed ? pts.length : pts.length - 1;
  let d = `M${n(pts[0][0])} ${n(pts[0][1])}`;
  for (let i = 0; i < last; i++) {
    const [p0, p1, p2, p3] = [P(i - 1), P(i), P(i + 1), P(i + 2)];
    d += ` C${n(p1[0] + (p2[0] - p0[0]) / 6)} ${n(p1[1] + (p2[1] - p0[1]) / 6)}`
      + ` ${n(p2[0] - (p3[0] - p1[0]) / 6)} ${n(p2[1] - (p3[1] - p1[1]) / 6)} ${n(p2[0])} ${n(p2[1])}`;
  }
  return closed ? d + " Z" : d;
}
const path = (d, fill, extra = "") => `<path d="${d}" fill="${fill}"${extra}/>`;
const poly = (pts) => "M" + pts.map(([x, y]) => `${n(x)} ${n(y)}`).join(" L") + " Z";

// An organic closed shape around (cx, cy): an ellipse whose radius wobbles
// with a few low harmonics plus a little per-point jitter.
function blob(r, cx, cy, rx, ry, rough = 0.14, count = 22) {
  const harm = [2, 3, 5, 7].map((k) => [k, (r() - 0.5) * rough * 2 / Math.sqrt(k), r() * Math.PI * 2]);
  const pts = [];
  for (let i = 0; i < count; i++) {
    const a = (i / count) * Math.PI * 2;
    let k = 1 + harm.reduce((s, [h, amp, ph]) => s + amp * Math.sin(h * a + ph), 0);
    k *= 1 + (r() - 0.5) * rough * 0.5;
    pts.push([cx + Math.cos(a) * rx * k, cy + Math.sin(a) * ry * k]);
  }
  return smooth(pts, true);
}

// A land line across the canvas (with a margin): base y and sine waves
// [[cycles across the canvas, amplitude px]].
function ridge(seed, base, waves) {
  const r = rng(seed);
  const ph = waves.map(() => r() * Math.PI * 2);
  return (x) => base + waves.reduce((y, [k, a], i) =>
    y + a * Math.sin((2 * Math.PI * k * x) / W + ph[i]), 0);
}
function hillD(line, bottom = H + 10) {
  const pts = [];
  for (let x = -40; x <= W + 40; x += 48) pts.push([x, line(x)]);
  return smooth(pts, false) + ` L${W + 40} ${bottom} L-40 ${bottom} Z`;
}

// A mountain range: fractal midpoint displacement between hand-placed peaks
// and saddles, so the ridgeline is jagged the way real ranges are.
function range(seed, keys, rough) {
  const r = rng(seed);
  let pts = keys.map(([x, y]) => [x, y]);
  let amp = rough;
  for (let pass = 0; pass < 6; pass++) {
    const next = [pts[0]];
    for (let i = 1; i < pts.length; i++) {
      const [x0, y0] = pts[i - 1];
      const [x1, y1] = pts[i];
      next.push([(x0 + x1) / 2 + (r() - 0.5) * amp * 0.3, (y0 + y1) / 2 + (r() - 0.5) * amp], pts[i]);
    }
    pts = next;
    amp *= 0.52;
  }
  return poly([...pts, [W + 40, H + 10], [-40, H + 10]]);
}

/* ---------------------------------------------------------- the painters */

// Deciduous tree: a tapered, forked trunk under a lumpy canopy lit from the
// upper left. hue = [light, main, shade].
function tree(r, x, ground, size, hue, trunk) {
  const [light, main, shade] = hue;
  const th = size * 0.95;
  const tw = size * 0.13;
  let s = path(poly([[x - tw, ground + 4], [x - tw * 0.45, ground - th], [x - size * 0.34, ground - th - size * 0.3],
    [x - size * 0.28, ground - th - size * 0.36], [x, ground - th - size * 0.1], [x + size * 0.3, ground - th - size * 0.4],
    [x + size * 0.36, ground - th - size * 0.33], [x + tw * 0.45, ground - th], [x + tw, ground + 4]]), trunk);
  const cy = ground - th - size * 0.55;
  s += path(blob(r, x + size * 0.05, cy + size * 0.06, size * 1.02, size * 0.86), shade);
  s += path(blob(r, x - size * 0.06, cy - size * 0.04, size * 0.9, size * 0.74), main);
  for (let i = 0; i < 4; i++) {
    const a = -2.6 + i * 0.75 + (r() - 0.5) * 0.3;
    s += path(blob(r, x + Math.cos(a) * size * 0.7, cy + Math.sin(a) * size * 0.55, size * 0.34, size * 0.28), i < 2 ? light : main);
  }
  s += path(blob(r, x - size * 0.3, cy - size * 0.28, size * 0.36, size * 0.28), light);
  return s;
}

// Spruce silhouette: a narrow spire over a dozen uneven tiers whose branch
// tips droop, each side jittered on its own.
function spruce(r, x, ground, h, fill, shade) {
  const tiers = 9 + Math.floor(r() * 5);
  const top = ground - h;
  const side = (dir) => {
    const pts = [];
    for (let i = 1; i <= tiers; i++) {
      const t = i / tiers;
      const y = top + h * 0.06 + h * 0.84 * Math.pow(t, 1.05);
      const w = h * 0.27 * Math.pow(t, 0.9) * (0.8 + r() * 0.4);
      pts.push([x + dir * w, y + h * 0.018 * (0.5 + r())]);                      // drooping tip
      if (i < tiers) pts.push([x + dir * w * (0.3 + r() * 0.15), y - h * 0.012]); // tuck back in
    }
    return pts;
  };
  const right = side(1);
  const left = side(-1).reverse();
  const tw = h * 0.025;
  const body = [[x, top], ...right, [x + tw, ground - h * 0.06], [x + tw, ground + 3],
    [x - tw, ground + 3], [x - tw, ground - h * 0.06], ...left];
  let s = path(poly(body), fill);
  if (shade) s += path(poly([[x, top], ...right, [x, ground - h * 0.06]]), shade);
  return s;
}

// Aspen: a tall narrow golden crown on a pale trunk with dark eyes.
function aspen(r, x, ground, h, hue, bark, mark) {
  const [light, main, shade] = hue;
  const w = h * 0.2;
  let s = `<rect x="${n(x - w * 0.09)}" y="${n(ground - h * 0.62)}" width="${n(w * 0.18)}" height="${n(h * 0.62 + 4)}" fill="${bark}"/>`;
  for (let i = 0; i < 3; i++) {
    s += `<rect x="${n(x - w * 0.09)}" y="${n(ground - h * (0.12 + i * 0.14 + r() * 0.05))}" width="${n(w * 0.18)}" height="${n(h * 0.012 + 1)}" fill="${mark}"/>`;
  }
  const cy = ground - h * 0.66;
  s += path(blob(r, x + w * 0.1, cy, w * 1.05, h * 0.36, 0.2), shade);
  s += path(blob(r, x - w * 0.05, cy - h * 0.02, w * 0.9, h * 0.33, 0.2), main);
  s += path(blob(r, x - w * 0.32, cy - h * 0.12, w * 0.42, h * 0.14, 0.2), light);
  return s;
}

function pumpkin(x, y, rad, c) {
  const [light, main, shade, stem] = c;
  let s = "";
  const ribs = [[-0.62, 0.5, shade], [0.62, 0.5, shade], [-0.34, 0.58, main], [0.34, 0.58, main], [0, 0.6, main]];
  for (const [dx, k, col] of ribs) s += `<ellipse cx="${n(x + dx * rad)}" cy="${n(y)}" rx="${n(rad * k)}" ry="${n(rad * 0.8)}" fill="${col}"/>`;
  s += `<ellipse cx="${n(x - rad * 0.2)}" cy="${n(y - rad * 0.32)}" rx="${n(rad * 0.16)}" ry="${n(rad * 0.34)}" fill="${light}" opacity=".8"/>`;
  s += `<path d="M${n(x - rad * 0.06)} ${n(y - rad * 0.74)} q${n(rad * 0.02)} ${n(-rad * 0.3)} ${n(rad * 0.22)} ${n(-rad * 0.4)}" stroke="${stem}" stroke-width="${n(rad * 0.15)}" stroke-linecap="round" fill="none"/>`;
  s += `<path d="M${n(x + rad * 0.1)} ${n(y - rad * 0.9)} q${n(rad * 0.4)} ${n(-rad * 0.1)} ${n(rad * 0.5)} ${n(rad * 0.2)} q${n(-rad * 0.3)} ${n(rad * 0.1)} ${n(-rad * 0.5)} ${n(-rad * 0.2)}" fill="${stem}"/>`;
  return s;
}

function hayBale(x, y, w, c) {
  const [main, shade, end] = c;
  const h = w * 0.62;
  return `<rect x="${n(x - w / 2)}" y="${n(y - h)}" width="${n(w)}" height="${n(h)}" rx="${n(h * 0.3)}" fill="${main}"/>`
    + `<rect x="${n(x - w / 2)}" y="${n(y - h * 0.35)}" width="${n(w)}" height="${n(h * 0.35)}" rx="${n(h * 0.18)}" fill="${shade}"/>`
    + `<ellipse cx="${n(x + w / 2 - h * 0.08)}" cy="${n(y - h / 2)}" rx="${n(h * 0.3)}" ry="${n(h / 2)}" fill="${end}"/>`
    + `<path d="M${n(x + w / 2 - h * 0.23)} ${n(y - h / 2)} a${n(h * 0.15)} ${n(h * 0.25)} 0 1 0 ${n(h * 0.3)} 0" stroke="${shade}" stroke-width="2" fill="none"/>`;
}

// Puffy cumulus for the playful look: lumpy top, flat base, shaded underside.
function puff(r, x, y, s, fill, shade) {
  const pts = [[x - 70 * s, y]];
  for (let i = 0; i <= 6; i++) pts.push([x - 60 * s + i * 20 * s, y - (18 + Math.sin(i / 6 * Math.PI) * 30 + (r() - 0.5) * 10) * s]);
  pts.push([x + 70 * s, y]);
  const d = smooth(pts, true);
  return path(d, shade, ` transform="translate(0 ${n(5 * s)})"`) + path(d, fill);
}
// Long soft stratus for the scenic and minimal looks.
function stratus(r, x, y, w, fill, op) {
  return path(blob(r, x, y, w, w * 0.07, 0.25, 26), fill, ` opacity="${op}"`);
}

function stars(seed, count, yMax, fill) {
  const r = rng(seed);
  let s = "";
  for (let i = 0; i < count; i++) {
    s += `<circle cx="${n(r() * W)}" cy="${n(r() * yMax)}" r="${n(r() < 0.1 ? 2 : 0.9 + r() * 0.8)}" fill="${fill}" opacity="${n(0.4 + r() * 0.55)}"/>`;
  }
  return s;
}

function birds(x, y, fill) {
  const b = (bx, by, k) => `<path d="M${n(bx)} ${n(by)} q${n(6 * k)} ${n(-6 * k)} ${n(13 * k)} ${n(-1 * k)} q${n(6 * k)} ${n(-7 * k)} ${n(13 * k)} ${n(1 * k)}" stroke="${fill}" stroke-width="${n(2 * k)}" fill="none" stroke-linecap="round"/>`;
  return b(x, y, 1) + b(x + 34, y + 12, 0.8) + b(x - 26, y + 16, 0.7);
}

// Vertical gradient def.
function vgrad(id, stops) {
  return `<linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">`
    + stops.map((c, i) => `<stop offset="${n(i / (stops.length - 1))}" stop-color="${c}"/>`).join("")
    + `</linearGradient>`;
}

// Sky, sun or moon, glow: shared by every look. The sun sits in the wall's
// open top bar, right of centre and clear of the clock.
function sky(p, id, sx, sy) {
  const defs = vgrad(`${id}-sky`, p.sky)
    + `<radialGradient id="${id}-glow"><stop offset="0" stop-color="${p.glow}" stop-opacity=".8"/><stop offset=".35" stop-color="${p.glow}" stop-opacity=".28"/><stop offset="1" stop-color="${p.glow}" stop-opacity="0"/></radialGradient>`
    + `<radialGradient id="${id}-orb" cx=".42" cy=".4"><stop offset="0" stop-color="${p.orb[0]}"/><stop offset="1" stop-color="${p.orb[1]}"/></radialGradient>`;
  let s = `<rect width="${W}" height="${H}" fill="url(#${id}-sky)"/>`;
  if (p.night) s += stars(7, 170, 560, p.star);
  s += `<circle cx="${sx}" cy="${sy}" r="300" fill="url(#${id}-glow)"/>`;
  s += `<circle cx="${sx}" cy="${sy}" r="${p.night ? 40 : 50}" fill="url(#${id}-orb)"/>`;
  if (p.night) {
    s += `<circle cx="${sx + 11}" cy="${sy - 9}" r="7" fill="${p.orb[1]}" opacity=".6"/><circle cx="${sx - 12}" cy="${sy + 11}" r="5" fill="${p.orb[1]}" opacity=".55"/>`;
  }
  return { defs, s };
}

/* ---------------------------------------------------------------- scenes */

// Playful: a storybook farm.
function harvest(p, id) {
  const r = rng(101);
  const { defs, s: skyS } = sky(p, id, 1170, 58);
  let s = skyS;
  const d = defs + vgrad(`${id}-field`, p.fieldGrad) + vgrad(`${id}-near`, p.nearGrad);
  if (!p.night) {
    s += puff(r, 360, 60, 0.55, p.cloud, p.cloudShade) + puff(r, 680, 44, 0.45, p.cloud, p.cloudShade)
      + puff(r, 1400, 56, 0.5, p.cloud, p.cloudShade);
    s += birds(880, 52, p.bird);
  }
  const far = ridge(11, 600, [[1, 26], [3, 14]]);
  const mid = ridge(23, 668, [[1, 34], [2, 22], [5, 6]]);
  const field = ridge(37, 752, [[1, 30], [2, 18]]);
  const near = ridge(53, 900, [[1, 26], [3, 14]]);
  s += path(hillD(far), p.far);
  s += path(hillD(mid), p.mid);
  for (let x = 30; x < W; x += 58 + r() * 70) {
    s += tree(r, x, mid(x) + 8, 12 + r() * 12, pick(r, p.foliage), p.trunk);
  }
  s += path(hillD(field), `url(#${id}-field)`);
  for (let k = 1; k <= 5; k++) {
    const pts = [];
    for (let x = -40; x <= W + 40; x += 60) pts.push([x, field(x) + k * 26 + Math.sin(x / 90) * 3]);
    s += `<path d="${smooth(pts, false)}" stroke="${p.furrow}" stroke-width="3" fill="none" opacity=".5"/>`;
  }
  s += path(hillD(near), `url(#${id}-near)`);
  // the barn: in the open space under the cameras, and in a phone's centre crop
  const bx = 1120;
  const by = near(bx) + 70;
  s += `<rect x="${bx - 70}" y="${n(by - 84)}" width="140" height="84" fill="${p.barn}"/>`;
  s += `<rect x="${bx + 20}" y="${n(by - 84)}" width="50" height="84" fill="${p.barnShade}"/>`;
  s += path(poly([[bx - 84, by - 80], [bx, by - 140], [bx + 84, by - 80]]), p.roof);
  s += path(poly([[bx, by - 140], [bx + 84, by - 80], [bx + 40, by - 80]]), p.roofShade);
  s += `<rect x="${bx - 24}" y="${n(by - 58)}" width="48" height="58" fill="${p.barnDoor}"/>`;
  s += `<path d="M${bx - 24} ${n(by - 58)} L${bx + 24} ${n(by)} M${bx + 24} ${n(by - 58)} L${bx - 24} ${n(by)} M${bx - 24} ${n(by - 58)} h48 v58 h-48 z" stroke="${p.trim}" stroke-width="4" fill="none"/>`;
  s += `<rect x="${bx - 14}" y="${n(by - 120)}" width="28" height="22" rx="2" fill="${p.window}" stroke="${p.trim}" stroke-width="3"/>`;
  s += `<rect x="${bx + 82}" y="${n(by - 124)}" width="42" height="124" rx="6" fill="${p.silo}"/>`;
  s += `<rect x="${bx + 106}" y="${n(by - 124)}" width="18" height="124" fill="${p.siloShade}"/>`;
  s += path(`M${bx + 80} ${n(by - 122)} a23 18 0 0 1 46 0 z`, p.roof);
  s += hayBale(1330, near(1330) + 56, 86, p.hay) + hayBale(1440, near(1440) + 72, 70, p.hay);
  s += tree(r, 1600, near(1600) + 30, 64, p.foliage[0], p.trunk);
  s += tree(r, 120, near(120) + 50, 88, p.foliage[1], p.trunk);
  s += tree(r, 330, near(330) + 70, 58, p.foliage[2], p.trunk);
  const patch = [[70, 1010, 34], [150, 1046, 28], [230, 1000, 38], [320, 1040, 30], [860, 1020, 30],
    [930, 1052, 24], [1010, 1030, 34], [1100, 1060, 26], [470, 1060, 22], [1210, 1034, 28]];
  for (const [x, y, rr] of patch) s += pumpkin(x, y, rr, p.pumpkin);
  return { defs: d, s };
}

// Scenic: a mountain lake in the aspens, painted with atmospheric depth.
function woodland(p, id) {
  const r = rng(303);
  const { defs, s: skyS } = sky(p, id, 1230, 50);
  let s = skyS;
  const d = defs + vgrad(`${id}-far`, p.farRange) + vgrad(`${id}-mid`, p.midRange)
    + vgrad(`${id}-lake`, p.lake) + vgrad(`${id}-shore`, p.shore)
    + `<linearGradient id="${id}-haze" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${p.haze}" stop-opacity="0"/><stop offset=".6" stop-color="${p.haze}" stop-opacity=".7"/><stop offset="1" stop-color="${p.haze}" stop-opacity="0"/></linearGradient>`
    + `<clipPath id="${id}-water"><rect x="0" y="744" width="${W}" height="140"/></clipPath>`;
  if (!p.night) {
    s += stratus(r, 520, 42, 150, p.cloud, ".75") + stratus(r, 780, 62, 110, p.cloud, ".6")
      + stratus(r, 1450, 36, 130, p.cloud, ".7");
    s += birds(900, 50, p.bird);
  }
  s += path(range(311, [[-40, 520], [180, 380], [420, 470], [640, 330], [900, 450], [1120, 350], [1380, 460], [1620, 360], [1960, 480]], 90), `url(#${id}-far)`);
  s += path(range(313, [[-40, 600], [260, 500], [560, 580], [820, 480], [1100, 570], [1400, 470], [1700, 560], [1960, 520]], 70), `url(#${id}-mid)`);
  s += `<rect x="0" y="520" width="${W}" height="220" fill="url(#${id}-haze)"/>`;
  // the far shore: a band of spruce with golden aspen stands, reflected below
  const shoreLine = ridge(83, 734, [[2, 6], [5, 3]]);
  let forest = path(hillD(shoreLine, 760), p.farForest);
  for (let x = -10; x < W + 10; x += 9 + r() * 9) {
    if (r() < 0.28) forest += path(blob(r, x, shoreLine(x) - 18, 12 + r() * 8, 20 + r() * 10, 0.3, 14), pick(r, p.farGold));
    else forest += spruce(r, x, shoreLine(x) + 4, 34 + r() * 30, p.farForest);
  }
  s += `<g id="${id}-shore-trees">${forest}</g>`;
  s += `<rect x="0" y="744" width="${W}" height="140" fill="url(#${id}-lake)"/>`;
  s += `<g clip-path="url(#${id}-water)" opacity=".28"><use href="#${id}-shore-trees" transform="translate(0 1498) scale(1 -1)"/></g>`;
  for (let i = 0; i < 26; i++) {
    const w = 40 + r() * 160;
    s += `<rect x="${n(r() * W)}" y="${n(760 + r() * 110)}" width="${n(w)}" height="2" rx="1" fill="${p.shimmer}" opacity="${n(0.15 + r() * 0.35)}"/>`;
  }
  // near shore
  const near = ridge(89, 880, [[1, 18], [2, 10]]);
  s += path(hillD(near), `url(#${id}-shore)`);
  // low autumn shrubs half-sunk into the bank, darker at the base
  for (let x = 470; x < 1520; x += 60 + r() * 80) {
    const w = 16 + r() * 14;
    s += path(blob(r, x + 3, near(x) + 12, w, w * 0.5, 0.35, 16), p.shrubShade);
    s += path(blob(r, x, near(x) + 8, w * 0.9, w * 0.42, 0.35, 16), pick(r, p.shrub));
  }
  // lower-left: a stand of aspens in front of tall spruce; right: the same, mirrored
  s += spruce(r, 60, near(60) + 60, 470, p.spruce, p.spruceShade);
  s += spruce(r, 230, near(230) + 80, 330, p.spruce, p.spruceShade);
  for (const [x, h] of [[300, 260], [370, 300], [150, 230], [440, 210]]) s += aspen(r, x, near(x) + 90, h, pick(r, p.aspen), p.bark, p.barkMark);
  s += spruce(r, 1830, near(1830) + 60, 480, p.spruce, p.spruceShade);
  s += spruce(r, 1660, near(1660) + 80, 320, p.spruce, p.spruceShade);
  for (const [x, h] of [[1300, 230], [1380, 270], [1480, 220], [1560, 250]]) s += aspen(r, x, near(x) + 90, h, pick(r, p.aspen), p.bark, p.barkMark);
  for (let i = 0; i < 70; i++) {
    const x = r() * W;
    s += `<ellipse cx="${n(x)}" cy="${n(near(x) + 60 + r() * 140)}" rx="${n(3 + r() * 3)}" ry="${n(1.6 + r())}" fill="${pick(r, p.fallen)}" opacity=".9"/>`;
  }
  return { defs: d, s };
}

// Minimal: smooth gradient hills, a hint of trees on one crest, leaves drifting.
function maple(p, id) {
  const r = rng(202);
  const { defs, s: skyS } = sky(p, id, 1240, 64);
  let s = skyS;
  let d = defs;
  if (!p.night) s += stratus(r, 460, 40, 170, p.cloud, ".55") + stratus(r, 1450, 58, 140, p.cloud, ".5");
  const lines = [ridge(61, 640, [[1, 30], [2, 12]]), ridge(64, 730, [[1, 40], [2, 16]]),
    ridge(67, 830, [[1, 44], [3, 12]]), ridge(71, 930, [[1, 36], [2, 14]])];
  lines.forEach((line, i) => {
    d += vgrad(`${id}-h${i}`, p.hills[i]);
    s += path(hillD(line), `url(#${id}-h${i})`);
    if (i === 1) {
      // a quiet row of small maples along the second crest
      for (let x = 20; x < W; x += 46 + r() * 70) {
        s += tree(r, x, line(x) + 3, 7 + r() * 6, pick(r, p.crest), p.crestTrunk);
      }
    }
  });
  return { defs: d, s };
}

/* -------------------------------------------------------------- palettes */
// Happy colors, day and evening. Evenings are blue hour with a golden
// horizon, never an ember-red wash.

const FOLIAGE_DAY = [
  ["#FFD56B", "#F7B733", "#E09A1E"],   // gold
  ["#FFB067", "#F58A2E", "#D96F1C"],   // orange
  ["#FF9A78", "#EE6446", "#CF4E33"],   // coral-red
  ["#D3E07A", "#A6C24A", "#86A332"],   // lingering green
];
const FOLIAGE_EVE = [
  ["#F7C85E", "#DDA43A", "#B98224"],
  ["#F4A461", "#D9803A", "#B4652A"],
  ["#EE8E6E", "#CE6A52", "#A9523F"],
  ["#B5C565", "#879C45", "#687C34"],
];

const PALETTES = {
  "fall-harvest": {
    day: { sky: ["#63BCEE", "#A6DAF6", "#E8F6FB"], glow: "#FFF1B8", orb: ["#FFF3B0", "#FFD35C"], cloud: "#FFFFFF", cloudShade: "#D3E8F4", bird: "#4A6A86",
      far: "#A3CBDD", mid: "#8CC46A", fieldGrad: ["#F6CF6A", "#E8B24A"], furrow: "#D49A36", nearGrad: ["#86C063", "#6BA84E"],
      foliage: FOLIAGE_DAY, trunk: "#7A5438", barn: "#D9534F", barnShade: "#C0443F", roof: "#7F3A37", roofShade: "#6A2F2D", barnDoor: "#B8413E", trim: "#FFF6EA", window: "#FFF6EA", silo: "#CBD5DC", siloShade: "#AEB9C2",
      hay: ["#F2CF6B", "#E0B64E", "#E8C360"], pumpkin: ["#FFC990", "#F58A2E", "#D96F1C", "#4E8A3A"] },
    eve: { sky: ["#1E2A5A", "#4B4D93", "#F2B98A"], glow: "#FFD9A0", orb: ["#FFF8DC", "#EFE0B6"], star: "#FFFFFF",
      far: "#4F5E97", mid: "#3F6E63", fieldGrad: ["#CFA35C", "#B08543"], furrow: "#96713A", nearGrad: ["#33604F", "#264A3E"],
      foliage: FOLIAGE_EVE, trunk: "#4A3628", barn: "#A8464A", barnShade: "#8E3A3E", roof: "#5E2F34", roofShade: "#4C262A", barnDoor: "#8A3A3E", trim: "#E9DCCB", window: "#FFD66B", silo: "#8C95AE", siloShade: "#737C96",
      hay: ["#C9A457", "#A88742", "#BC9A50"], pumpkin: ["#F7B77A", "#E07B2A", "#B9611F", "#3F6E3A"] },
  },
  "fall-woodland": {
    day: { sky: ["#4E9FD9", "#8CC4EA", "#CFE8F3", "#F4EAD2"], glow: "#FFF4C7", orb: ["#FFF8D6", "#FFE08A"], cloud: "#FFFFFF", bird: "#4F6B84",
      farRange: ["#9DB0D6", "#C9D5E8"], midRange: ["#7C94C0", "#A9BAD8"], haze: "#EEF4F7",
      farForest: "#5D8C7B", farGold: ["#E3C160", "#EDCB6E", "#D9B04E"],
      lake: ["#A6D6EC", "#62A9D0"], shimmer: "#FFFFFF", shore: ["#7FB35E", "#5A9249"],
      shrub: ["#F08A3C", "#E86F3A", "#F4B447"], shrubShade: "#4E8540", spruce: "#2F6B52", spruceShade: "#265A44",
      aspen: [["#FFE58A", "#F9C93C", "#DFA928"], ["#FFD56B", "#F7B733", "#E09A1E"]], bark: "#F2EFE6", barkMark: "#5A5A5A",
      fallen: ["#F7B733", "#FFD56B", "#F58A2E"] },
    eve: { sky: ["#18224E", "#3A4788", "#8A7EB8", "#F2C49A"], glow: "#FFE0A8", orb: ["#FFF8DC", "#EFE0B6"], star: "#FFFFFF",
      farRange: ["#5B69A2", "#8088BC"], midRange: ["#434F8A", "#626DA3"], haze: "#A9ABD4",
      farForest: "#34506A", farGold: ["#C9A04A", "#D6AE55", "#B98F3F"],
      lake: ["#7A7FBA", "#2F3E79"], shimmer: "#FFF3CC", shore: ["#33574B", "#22403A"],
      shrub: ["#D4773A", "#C8653A", "#D9A043"], shrubShade: "#1E3A33", spruce: "#1E3B3F", spruceShade: "#183233",
      aspen: [["#F7D476", "#E3B842", "#BF9430"], ["#F7C85E", "#DDA43A", "#B98224"]], bark: "#D8D2C6", barkMark: "#3A3A44",
      fallen: ["#DDA43A", "#F2D06A", "#D9803A"] },
  },
  "fall-maple": {
    day: { sky: ["#7FC8F0", "#C4E6F6", "#FFF0D2"], glow: "#FFF1C0", orb: ["#FFF8DA", "#FFDF8A"], cloud: "#FFFFFF",
      hills: [["#D5E6C0", "#BFD8A6"], ["#F5D891", "#EBC169"], ["#F4BD8A", "#E7A06A"], ["#A3CE8E", "#80B56E"]],
      crest: [["#FFD56B", "#F7B733", "#E09A1E"], ["#FFB067", "#F58A2E", "#D96F1C"], ["#FF9A78", "#EE6446", "#CF4E33"]], crestTrunk: "#9A7A5A" },
    eve: { sky: ["#232C66", "#655FA6", "#F4C6A2"], glow: "#FFE2B0", orb: ["#FFF8DC", "#EFE0B6"], star: "#FFFFFF",
      hills: [["#8784C0", "#7471B0"], ["#A18BC0", "#8B76AE"], ["#5E84B6", "#4C70A2"], ["#35587E", "#284662"]],
      crest: [["#F7C85E", "#DDA43A", "#B98224"], ["#F4A461", "#D9803A", "#B4652A"]], crestTrunk: "#5E5478" },
  },
};
for (const look of Object.values(PALETTES)) look.eve.night = true;

const SCENES = { "fall-harvest": harvest, "fall-woodland": woodland, "fall-maple": maple };

mkdirSync(OUT, { recursive: true });
for (const [look, draw] of Object.entries(SCENES)) {
  for (const variant of ["day", "eve"]) {
    const id = `${look.replace("fall-", "")}-${variant}`;
    const { defs, s } = draw(PALETTES[look][variant], id);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMax slice">`
      + `<defs>${defs}</defs>${s}</svg>\n`;
    const name = `${look}-${variant}.svg`;
    writeFileSync(join(OUT, name), svg);
    if (!process.env.SEASON_ART_OUT) console.log(`${name}  ${(svg.length / 1024).toFixed(1)} KB`);
  }
}
