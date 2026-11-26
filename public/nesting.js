/* Nesting / imposition maths for the Dimension Finder.
 *
 * No DOM: runs in the browser (window.Nesting) and in Node (module.exports), so it can be
 * tested on its own and ported as-is.
 *
 * Sheet work (folding cartons, commercial print)
 *   The die outline is drawn into a bitmap (32 px per inch), the inside is filled, and the shape
 *   is grown by half the gutter so two neighbours always keep a full gutter between them. Copies
 *   are placed in rows; each next row may be shifted sideways and/or turned 180 degrees
 *   (head-to-tail) and is dropped as close as the outline allows. Every orientation, shift and
 *   mode is counted on the usable sheet (sheet minus gripper, tail and side margins) and the most
 *   ups wins. Rectangles (commercial print, or a carton with no dieline outline) use an exact
 *   rectangle fill that can also mix turned pieces into the leftover strip.
 *
 * Web work (pouches, roll labels)
 *   Lanes across the usable web width; repeat along the web. Both orientations.
 */
(function (root) {
  "use strict";

  const RES = 32; // bitmap pixels per inch

  const DEFAULTS = {
    sheets: [
      { id: "o2840", name: "28 × 40", press: "Offset", w: 40, h: 28, gripper: 0.5, tail: 0.375, side: 0.25 },
      { id: "o2436", name: "24 × 36", press: "Offset", w: 36, h: 24, gripper: 0.5, tail: 0.375, side: 0.25 },
      { id: "d2081", name: "20.8125 × 29.5", press: "Digital", w: 29.5, h: 20.8125, gripper: 0.375, tail: 0.25, side: 0.25 },
    ],
    gutter: { folding_carton: 0.25, commercial: 0.25 },
    web: {
      pouch: { web: 30, usable: 29, gapAcross: 0.25, gapAround: 0.125 },
      roll_label: { web: 13, usable: 12, gapAcross: 0.125, gapAround: 0.125 },
    },
  };

  // ------------------------------------------------------------------ die outline

  /** Die from the analyzer's overlay (page points from the top-left). Falls back to a rectangle. */
  function dieFromOverlay(overlay, flatW, flatH) {
    const cut = (overlay && overlay.cut) || [];
    const t = overlay && overlay.trim;
    if (cut.length >= 4) {
      const tol = 4;
      const inside = t
        ? (x, y) => x >= t[0] - tol && x <= t[0] + t[2] + tol && y >= t[1] - tol && y <= t[1] + t[3] + tol
        : () => true;
      const segs = cut.filter((s) => inside(s[0], s[1]) && inside(s[2], s[3]));
      if (segs.length >= 4) {
        let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
        for (const s of segs) {
          x0 = Math.min(x0, s[0], s[2]); x1 = Math.max(x1, s[0], s[2]);
          y0 = Math.min(y0, s[1], s[3]); y1 = Math.max(y1, s[1], s[3]);
        }
        const local = segs.map((s) => s.map((v, i) => (i % 2 ? v - y0 : v - x0) / 72));
        return { w: (x1 - x0) / 72, h: (y1 - y0) / 72, segs: local, source: "dieline" };
      }
    }
    return rectDie(flatW, flatH);
  }

  function rectDie(w, h) {
    return { w, h, segs: [[0, 0, w, 0], [w, 0, w, h], [w, h, 0, h], [0, h, 0, 0]], source: "rectangle" };
  }

  function bezier(s, t) {
    const [x0, y0, x1, y1, c1x, c1y, c2x, c2y] = s, u = 1 - t;
    return [u * u * u * x0 + 3 * u * u * t * c1x + 3 * u * t * t * c2x + t * t * t * x1,
            u * u * u * y0 + 3 * u * u * t * c1y + 3 * u * t * t * c2y + t * t * t * y1];
  }

  /** Filled bitmap of the die. Returns null when the outline is open (the fill leaks). */
  function rasterize(die) {
    const P = 3;
    const W = Math.ceil(die.w * RES) + 2 * P + 1, H = Math.ceil(die.h * RES) + 2 * P + 1;
    const line = new Uint8Array(W * H);
    const plot = (x, y) => {
      const xi = Math.round(x * RES) + P, yi = Math.round(y * RES) + P;
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
        const a = xi + dx, b = yi + dy;
        if (a >= 0 && a < W && b >= 0 && b < H) line[b * W + a] = 1;
      }
    };
    for (const s of die.segs) {
      const pts = s.length >= 8 ? Array.from({ length: 17 }, (_, i) => bezier(s, i / 16)) : [[s[0], s[1]], [s[2], s[3]]];
      for (let i = 1; i < pts.length; i++) {
        const [ax, ay] = pts[i - 1], [bx, by] = pts[i];
        const n = Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) * RES * 2));
        for (let j = 0; j <= n; j++) plot(ax + (bx - ax) * j / n, ay + (by - ay) * j / n);
      }
    }
    // flood the outside from the border
    const out = new Uint8Array(W * H), stack = [];
    const push = (i) => { if (!line[i] && !out[i]) { out[i] = 1; stack.push(i); } };
    for (let x = 0; x < W; x++) { push(x); push((H - 1) * W + x); }
    for (let y = 0; y < H; y++) { push(y * W); push(y * W + W - 1); }
    while (stack.length) {
      const i = stack.pop(), x = i % W;
      if (x > 0) push(i - 1);
      if (x < W - 1) push(i + 1);
      if (i >= W) push(i - W);
      if (i < W * (H - 1)) push(i + W);
    }
    let minx = W, miny = H, maxx = -1, maxy = -1, area = 0;
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) if (!out[y * W + x]) {
      area++; if (x < minx) minx = x; if (x > maxx) maxx = x; if (y < miny) miny = y; if (y > maxy) maxy = y;
    }
    if (maxx < 0 || area < 0.3 * die.w * die.h * RES * RES) return null;
    const w = maxx - minx + 1, h = maxy - miny + 1, data = new Uint8Array(w * h);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) data[y * w + x] = out[(y + miny) * W + x + minx] ? 0 : 1;
    return { w, h, data, area, ox: (minx - P) / RES, oy: (miny - P) / RES };
  }

  /** Quarter turns clockwise (y down). */
  function rotate(m, k) {
    k = ((k % 4) + 4) % 4;
    if (!k) return m;
    const w = k % 2 ? m.h : m.w, h = k % 2 ? m.w : m.h, data = new Uint8Array(w * h);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      let sx, sy;
      if (k === 1) { sx = y; sy = m.h - 1 - x; }
      else if (k === 2) { sx = m.w - 1 - x; sy = m.h - 1 - y; }
      else { sx = m.w - 1 - y; sy = x; }
      data[y * w + x] = m.data[sy * m.w + sx];
    }
    return { ...m, w, h, data };
  }

  /** Grow by a disc of radius r pixels. The result is r pixels bigger on each side. */
  function dilate(m, r) {
    if (r <= 0) return m;
    const w = m.w + 2 * r, h = m.h + 2 * r;
    // horizontal runs: pre[y][x] = number of set pixels in row y left of x (padded frame)
    const pre = new Int32Array(h * (w + 1));
    for (let y = 0; y < h; y++) {
      const sy = y - r;
      for (let x = 0; x < w; x++) {
        const sx = x - r;
        const v = sy >= 0 && sy < m.h && sx >= 0 && sx < m.w ? m.data[sy * m.w + sx] : 0;
        pre[y * (w + 1) + x + 1] = pre[y * (w + 1) + x] + v;
      }
    }
    const data = new Uint8Array(w * h);
    for (let dy = -r; dy <= r; dy++) {
      const hw = Math.floor(Math.sqrt(r * r - dy * dy));
      for (let y = 0; y < h; y++) {
        const yy = y + dy;
        if (yy < 0 || yy >= h) continue;
        const row = yy * (w + 1);
        for (let x = 0; x < w; x++) {
          if (data[y * w + x]) continue;
          const a = Math.max(0, x - hw), b = Math.min(w, x + hw + 1);
          if (pre[row + b] - pre[row + a] > 0) data[y * w + x] = 1;
        }
      }
    }
    return { w, h, data };
  }

  function profiles(d) {
    const top = new Int32Array(d.w).fill(-1), bot = new Int32Array(d.w).fill(-1);
    let px = 0;
    for (let y = 0; y < d.h; y++) {
      let l = -1, rr = -1;
      for (let x = 0; x < d.w; x++) if (d.data[y * d.w + x]) {
        if (l < 0) l = x; rr = x;
        if (top[x] < 0) top[x] = y;
        bot[x] = y;
      }
      if (l >= 0) px = Math.max(px, rr - l + 1);
    }
    return { top, bot, px };
  }

  function periodic(prof, px, useMax) {
    const a = new Float64Array(px).fill(useMax ? -Infinity : Infinity);
    for (let x = 0; x < prof.length; x++) {
      if (prof[x] < 0) continue;
      const i = x % px;
      a[i] = useMax ? Math.max(a[i], prof[x]) : Math.min(a[i], prof[x]);
    }
    return a;
  }

  /** Smallest drop of a row of `lower` copies (shifted sx) under a row of `upper` copies, both at pitch px. */
  function drop(Bp, Tp, px, sx) {
    let best = -Infinity;
    for (let i = 0; i < px; i++) {
      const j = (((i - sx) % px) + px) % px;
      if (Bp[i] > -Infinity && Tp[j] < Infinity) best = Math.max(best, Bp[i] - Tp[j]);
    }
    return Math.max(1, best + 1);
  }

  // ------------------------------------------------------------------ sheet nesting

  function usableOf(sheet) {
    return { w: sheet.w - 2 * sheet.side, h: sheet.h - sheet.gripper - sheet.tail };
  }

  /** Count a lattice of rows on a Wu x Hu pixel area; returns {ups, rows, g}. */
  function countLattice(rows, px, w0, h0, Wu, Hu) {
    const live = rows.filter((r) => r.y + h0 <= Hu);
    if (!live.length) return { ups: 0, rows: [], g: 0 };
    const step = Math.max(1, Math.round(px / 48));
    let best = { ups: -1, g: 0 };
    for (let g = 0; g < px; g += step) {
      let ups = 0;
      for (const r of live) {
        const first = (((r.s + g) % px) + px) % px;
        if (first + w0 <= Wu) ups += Math.floor((Wu - w0 - first) / px) + 1;
      }
      if (ups > best.ups) best = { ups, g };
    }
    return { ups: best.ups, rows: live, g: best.g };
  }

  function nestMask(mask, gutterIn, usable) {
    const r = Math.max(0, Math.round((gutterIn / 2) * RES));
    const Wu = Math.floor(usable.w * RES + 1e-6), Hu = Math.floor(usable.h * RES + 1e-6);
    let best = null;
    for (const kA of [0, 1]) {
      const A = rotate(mask, kA), C = rotate(mask, kA + 2);
      const dA = dilate(A, r), dC = dilate(C, r);
      const pA = profiles(dA), pC = profiles(dC);
      const px = Math.max(pA.px, pC.px);
      const BA = periodic(pA.bot, px, true), TA = periodic(pA.top, px, false);
      const BC = periodic(pC.bot, px, true), TC = periodic(pC.top, px, false);
      const w0 = A.w, h0 = A.h;
      const step = Math.max(1, Math.round(px / 64));
      const sameAA0 = drop(BA, TA, px, 0), sameCC0 = drop(BC, TC, px, 0);
      for (const mode of ["same", "alt"]) {
        for (let sx = 0; sx < px; sx += step) {
          const rows = [];
          if (mode === "same") {
            const py = Math.max(drop(BA, TA, px, sx), Math.ceil(drop(BA, TA, px, (2 * sx) % px) / 2));
            for (let i = 0; i * py + h0 <= Hu; i++) rows.push({ y: i * py, s: (i * sx) % px, k: kA });
          } else {
            const y1 = drop(BA, TC, px, sx), y2 = drop(BC, TA, px, -sx);
            const P2 = Math.max(y1 + y2, sameAA0, sameCC0);
            for (let i = 0; ; i++) {
              const ya = i * P2, yc = i * P2 + y1;
              if (ya + h0 > Hu) break;
              rows.push({ y: ya, s: 0, k: kA });
              if (yc + h0 <= Hu) rows.push({ y: yc, s: sx, k: kA + 2 });
            }
          }
          const c = countLattice(rows, px, w0, h0, Wu, Hu);
          const height = c.rows.length ? Math.max(...c.rows.map((q) => q.y)) + h0 : 0;
          if (!best || c.ups > best.ups || (c.ups === best.ups && height < best.height)) {
            best = { ups: c.ups, height, rows: c.rows, g: c.g, px, w0, h0, kA, mode, sx };
          }
        }
      }
    }
    if (!best || best.ups <= 0) return { ups: 0, placements: [] };
    const placements = [];
    for (const r of best.rows) {
      const first = (((r.s + best.g) % best.px) + best.px) % best.px;
      for (let x = first; x + best.w0 <= Wu; x += best.px) placements.push({ x: x / RES, y: r.y / RES, k: ((r.k % 4) + 4) % 4 });
    }
    const turned = best.rows.some((q) => q.k !== best.kA);
    // Rows closer together than the die's own height (plus gutter): flaps sit between the next row's flaps.
    const interlocked = best.rows.length > 1 && best.rows[1].y - best.rows[0].y < best.h0 + 2 * r - 1;
    const shifted = best.mode === "same" && best.sx > 0 && best.rows.length > 1;
    return {
      ups: placements.length,
      placements,
      rows: best.rows.length,
      pieceW: best.w0 / RES,
      pieceH: best.h0 / RES,
      layout: turned ? "head-to-tail (every other row turned 180°)" : shifted ? "staggered rows" : interlocked ? "interlocking rows (flaps tuck between rows)" : "straight rows",
      turned90: best.kA === 1,
    };
  }

  /** Exact rectangle fill, optionally mixing turned pieces into the leftover strip. */
  function nestRect(pw, ph, gutter, usable, allowMix) {
    const fit = (len, n) => (n > len + 1e-9 ? 0 : Math.floor((len + gutter + 1e-9) / (n + gutter)));
    const grid = (x0, y0, W, H, a, b, k) => {
      const nx = fit(W, a), ny = fit(H, b), out = [];
      for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) out.push({ x: x0 + i * (a + gutter), y: y0 + j * (b + gutter), k });
      return out;
    };
    let best = null;
    const consider = (pl, label, turned90) => {
      if (!best || pl.length > best.placements.length) best = { placements: pl, layout: label, turned90 };
    };
    for (const [a, b, k] of [[pw, ph, 0], [ph, pw, 1]]) {
      consider(grid(0, 0, usable.w, usable.h, a, b, k), "straight rows", k === 1);
      if (!allowMix) continue;
      const nx = fit(usable.w, a), ny = fit(usable.h, b);
      for (let c = 1; c < nx; c++) { // c columns as-is, the rest of the width turned
        const used = c * (a + gutter);
        consider([...grid(0, 0, used - gutter, usable.h, a, b, k), ...grid(used, 0, usable.w - used, usable.h, b, a, 1 - k)],
          "mixed: some pieces turned 90° to fill the side strip", k === 1);
      }
      for (let rr = 1; rr < ny; rr++) { // rr rows as-is, the rest of the height turned
        const used = rr * (b + gutter);
        consider([...grid(0, 0, usable.w, used - gutter, a, b, k), ...grid(0, used, usable.w, usable.h - used, b, a, 1 - k)],
          "mixed: some pieces turned 90° to fill the end strip", k === 1);
      }
    }
    return { ups: best.placements.length, placements: best.placements, pieceW: pw, pieceH: ph, layout: best.layout, turned90: best.turned90 };
  }

  const maskCache = new WeakMap();

  /**
   * Nest one die (from dieFromOverlay / rectDie) on each sheet.
   * Returns per sheet: {sheet, usable, ups, straightUps, layout, placements[{x,y,k}], utilisation}.
   */
  function nestSheets(die, sheets, gutter, opts = {}) {
    let mask = null;
    if (die.source === "dieline") {
      if (!maskCache.has(die)) maskCache.set(die, rasterize(die));
      mask = maskCache.get(die);
    }
    const dieArea = mask ? mask.area / (RES * RES) : die.w * die.h;
    return sheets.map((sheet) => {
      const usable = usableOf(sheet);
      const straight = nestRect(die.w, die.h, gutter, usable, false);
      let best = mask ? nestMask(mask, gutter, usable) : nestRect(die.w, die.h, gutter, usable, opts.mix !== false);
      if (mask && straight.ups > best.ups) best = { ...straight, rectFallback: true };
      return {
        sheet, usable,
        ups: best.ups,
        straightUps: straight.ups,
        layout: best.layout || "",
        turned90: !!best.turned90,
        placements: best.placements,
        pieceW: best.pieceW || die.w, pieceH: best.pieceH || die.h,
        // Placements are the top-left of this box (the die's bitmap, or its rectangle), turned by k.
        box: mask && !best.rectFallback ? { w: mask.w / RES, h: mask.h / RES } : { w: die.w, h: die.h },
        bitmapOffset: mask && !best.rectFallback ? { x: mask.ox, y: mask.oy } : { x: 0, y: 0 },
        mask: mask && !best.rectFallback ? mask : null,
        utilisation: best.ups ? (best.ups * dieArea) / (sheet.w * sheet.h) : 0,
        outline: mask ? "dieline" : "rectangle",
        openOutline: die.source === "dieline" && !mask,
      };
    });
  }

  /** Die segments turned k quarter turns inside its own box (w x h), for drawing. */
  function turnSegs(segs, w, h, k, ox = 0, oy = 0) {
    const t = (x, y) => {
      x -= ox; y -= oy;
      if (k === 1) return [h - y, x];
      if (k === 2) return [w - x, h - y];
      if (k === 3) return [y, w - x];
      return [x, y];
    };
    return segs.map((s) => {
      const out = [];
      for (let i = 0; i < s.length; i += 2) out.push(...t(s[i], s[i + 1]));
      return out;
    });
  }

  // ------------------------------------------------------------------ web (roll) layouts

  /** Lanes across the usable web and repeat along it, for both orientations; best first. */
  function nestWeb(itemW, itemH, s) {
    const opts = [[itemW, itemH, false], [itemH, itemW, true]].map(([across, around, turned]) => {
      const lanes = across > s.usable + 1e-9 ? 0 : Math.floor((s.usable + s.gapAcross + 1e-9) / (across + s.gapAcross));
      const repeat = around + s.gapAround;
      const used = lanes ? lanes * across + (lanes - 1) * s.gapAcross : 0;
      return {
        across, around, turned, lanes, repeat, usedWidth: used,
        perFoot: lanes ? (lanes * 12) / repeat : 0,
        perMeter: lanes ? (lanes * 39.3701) / repeat : 0,
        utilisation: lanes ? used / s.usable : 0,
      };
    });
    opts.sort((a, b) => b.perFoot - a.perFoot || b.lanes - a.lanes);
    return opts;
  }

  const api = { RES, DEFAULTS, dieFromOverlay, rectDie, rasterize, rotate, dilate, nestSheets, nestRect, nestWeb, turnSegs, usableOf };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Nesting = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
