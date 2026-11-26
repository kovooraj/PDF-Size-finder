// node --test tests/js
const test = require("node:test");
const assert = require("node:assert");
const N = require("../../public/nesting.js");
const fx = require("./carton_fixtures.json");

// Every placed die must sit inside the usable area and keep at least ~the gutter from its neighbours.
function checkNoOverlap(die, r, gutter) {
  const mask = N.rasterize(die);
  const R = N.RES, rad = Math.max(0, Math.round((gutter / 2) * R) - 1);
  const Wu = Math.floor(r.usable.w * R + 1e-6), Hu = Math.floor(r.usable.h * R + 1e-6);
  const sheet = new Uint8Array((Wu + 2 * rad + 2) * (Hu + 2 * rad + 2)), SW = Wu + 2 * rad + 2;
  for (const p of r.placements) {
    const m = N.dilate(N.rotate(mask, p.k), rad);
    const x0 = Math.round(p.x * R), y0 = Math.round(p.y * R);
    assert.ok(x0 >= 0 && y0 >= 0, "inside usable area (top-left)");
    assert.ok(x0 + m.w - 2 * rad <= Wu + 1 && y0 + m.h - 2 * rad <= Hu + 1, "inside usable area (bottom-right)");
    for (let y = 0; y < m.h; y++) for (let x = 0; x < m.w; x++) if (m.data[y * m.w + x]) {
      const i = (y0 + y) * SW + x0 + x;
      assert.ok(!sheet[i], `dies overlap or sit closer than the gutter (${r.sheet.name})`);
      sheet[i] = 1;
    }
  }
}

test("rectangles: straight grid counts match hand maths", () => {
  const usable = { w: 39.5, h: 27.125 };
  const r = N.nestRect(3.5, 2, 0.25, usable, false);
  assert.strictEqual(r.ups, 120); // 10 across x 12 down
  const mixed = N.nestRect(3.5, 2, 0.25, usable, true);
  assert.ok(mixed.ups >= 120);
});

test("sheet margins: gripper, tail and sides come off the sheet", () => {
  const s = N.DEFAULTS.sheets[0];
  assert.deepStrictEqual(N.usableOf(s), { w: 40 - 0.5, h: 28 - 0.5 - 0.375 });
});

test("cartons nest without overlap and never do worse than straight rows", () => {
  for (const [name, f] of Object.entries(fx)) {
    const die = N.dieFromOverlay(f.overlay, f.fields.flat_width, f.fields.flat_height);
    assert.strictEqual(die.source, "dieline", name);
    for (const r of N.nestSheets(die, N.DEFAULTS.sheets, 0.25)) {
      assert.ok(r.ups >= r.straightUps, `${name} ${r.sheet.name}`);
      if (r.outline === "dieline" && r.ups) checkNoOverlap(die, r, 0.25);
    }
  }
});

test("tuck-end carton gains ups by nesting head-to-tail", () => {
  const f = fx.rte_carton;
  const die = N.dieFromOverlay(f.overlay, f.fields.flat_width, f.fields.flat_height);
  const r = N.nestSheets(die, [N.DEFAULTS.sheets[0]], 0.25)[0];
  assert.ok(r.ups > r.straightUps, `nested ${r.ups} vs straight ${r.straightUps}`);
});

test("web lanes: 12in usable label web", () => {
  const [best] = N.nestWeb(3, 4, N.DEFAULTS.web.roll_label);
  assert.strictEqual(best.lanes, 3); // 3 x 3in + 2 x 0.125 gaps = 9.25 (4in across fits only 2)
  const pouch = N.nestWeb(6, 18, N.DEFAULTS.web.pouch);
  assert.ok(pouch.every(o => o.usedWidth <= 29));
});
