//
// tests/js/chart-series-colors.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Contract for the combined chart: stable categorical series colors from the central
// --ju-dv-N tokens, and one shared "Show peaks" state instead of per-series peak entries.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const read = (path) => readFileSync(new URL(`../../${path}`, import.meta.url), "utf8");
const dataSandbox = { window: {} };
vm.runInNewContext(read("app/static/js/charts/combined-chart-data.js"), dataSandbox);
const D = dataSandbox.window.CombinedChartData;
const plain = (value) => JSON.parse(JSON.stringify(value));

function loadChart(extra = {}) {
  const context = { window: {}, CombinedChartData: D, console, ...extra };
  vm.runInNewContext(read("app/static/js/charts/combined-chart.js"), context);
  return context;
}

// Theme blocks of gentelella-layout.css: the light :root block and the dark override.
const css = read("app/static/css/gentelella-layout.css");
function tokens(block) {
  const out = {};
  for (const [, name, value] of block.matchAll(/--(ju-(?:dv-\d+|panel-bg|st-[a-z]+))\s*:\s*([^;]+);/g)) out[name] = value.trim();
  return out;
}
const darkStart = css.indexOf('[data-bs-theme="dark"] {');
const lightTokens = tokens(css.slice(0, darkStart));
const darkTokens = tokens(css.slice(darkStart, css.indexOf("\n}", darkStart)));
const series = (t) => Array.from({ length: D.SERIES_SLOT_COUNT }, (_, i) => t[`ju-dv-${i + 1}`]);

const channel = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
const rgb = (hex) => {
  const full = hex.length === 4 ? `#${[...hex.slice(1)].map((c) => c + c).join("")}` : hex;
  return [1, 3, 5].map((i) => parseInt(full.slice(i, i + 2), 16));
};
const luminance = (hex) => { const [r, g, b] = rgb(hex).map(channel); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
const contrast = (a, b) => { const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
function lab(hex) {
  const [r, g, b] = rgb(hex).map(channel);
  const f = (t) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116);
  const x = f((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047);
  const y = f(0.2126 * r + 0.7152 * g + 0.0722 * b);
  const z = f((0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.089);
  return [116 * y - 16, 500 * (x - y), 200 * (y - z)];
}
const distance = (a, b) => Math.hypot(...lab(a).map((v, i) => v - lab(b)[i]));

test("every theme defines the same number of distinct, legible series tokens", () => {
  for (const [name, theme, background] of [["light", lightTokens, lightTokens["ju-panel-bg"]], ["dark", darkTokens, darkTokens["ju-panel-bg"]]]) {
    const colors = series(theme);
    assert.ok(colors.every((c) => /^#[0-9a-f]{6}$/i.test(c)), `${name}: all series tokens are hex colors`);
    assert.equal(new Set(colors).size, D.SERIES_SLOT_COUNT, `${name}: no duplicate series color`);
    colors.forEach((color, i) => assert.ok(contrast(color, background) >= 3, `${name}: --ju-dv-${i + 1} has 3:1 on the panel`));
    for (let i = 0; i < colors.length; i += 1) {
      for (let j = i + 1; j < colors.length; j += 1) {
        assert.ok(distance(colors[i], colors[j]) >= 15, `${name}: --ju-dv-${i + 1} and --ju-dv-${j + 1} are visually distinct`);
      }
    }
  }
});

test("series colors never reuse a status color", () => {
  const status = new Set(["#198754", "#ffc107", "#dc3545", "#0dcaf0", "#6c757d"]);
  for (const theme of [lightTokens, darkTokens]) {
    for (const [name, value] of Object.entries(theme)) if (name.startsWith("ju-st-")) status.add(value.toLowerCase());
  }
  const chart = loadChart();
  const palettes = [chart.COMBINED_PALETTES.light, chart.COMBINED_PALETTES.dark];
  palettes.forEach((palette) => Object.values(palette).forEach((value) => status.add(String(value).toLowerCase())));
  for (const theme of [lightTokens, darkTokens]) {
    series(theme).forEach((color) => assert.ok(!status.has(color.toLowerCase()), `${color} is a status color`));
  }
});

test("series slots are deterministic and independent of source order", () => {
  const keys = ["http", "ping", "tcp:587", "tcp:25", "tcp:443"];
  const first = plain(D.seriesSlots(keys));
  assert.deepEqual(plain(D.seriesSlots([...keys].reverse())), first);
  assert.deepEqual(plain(D.seriesSlots(keys)), first);
  assert.equal(first.http.slot, 0);
  assert.equal(first.ping.slot, 1);
  assert.deepEqual([first["tcp:25"].slot, first["tcp:443"].slot, first["tcp:587"].slot], [2, 3, 4]);
});

test("the first eight series get eight different slots, extra series change the dash lap", () => {
  const keys = ["http", "ping", "tcp:22", "tcp:25", "tcp:80", "tcp:110", "tcp:143", "tcp:443", "tcp:465", "tcp:587"];
  const slots = D.seriesSlots(keys);
  assert.equal(new Set(keys.slice(0, 8).map((k) => slots[k].slot)).size, 8);
  const pairs = keys.map((k) => `${slots[k].slot}/${slots[k].lap}`);
  assert.equal(new Set(pairs).size, keys.length, "slot and lap together stay unique");
  assert.equal(slots["tcp:587"].lap, 1);
});

test("the TCP aggregate series keeps its own stable slot", () => {
  assert.equal(D.seriesSlots(["http", "tcp"]).tcp.slot, 2);
});

test("palette reads the series tokens of the active theme at runtime", () => {
  const active = { value: series(lightTokens) };
  const chart = loadChart({ getCssVar: (name, fallback) => active.value[Number(name.split("-").pop()) - 1] ?? fallback });
  const light = chart._combinedPalette({ mode: "light", text: "#000000" });
  assert.deepEqual(plain(light.series), series(lightTokens));
  active.value = series(darkTokens);
  const dark = chart._combinedPalette({ mode: "dark", text: "#ffffff" });
  assert.deepEqual(plain(dark.series), series(darkTokens));
  assert.equal(dark.down, chart.COMBINED_PALETTES.dark.down);
});

test("the chart script holds no hard-coded series colors", () => {
  const chartSource = read("app/static/js/charts/combined-chart.js");
  const hexes = new Set(series(lightTokens).concat(series(darkTokens)));
  for (const hex of hexes) assert.ok(!chartSource.includes(hex), `${hex} is hard-coded in combined-chart.js`);
  assert.doesNotMatch(chartSource, /palette\[(src\.id|ds\._srcId)\]/);
});

test("peak visibility: one switch drives all peaks, hidden series show none, axis follows", () => {
  const both = [{ msVisible: true, hasPeakData: true }, { msVisible: true, hasPeakData: true }];
  assert.deepEqual(plain(D.peakVisibility(both, true)), { visible: [true, true], axisVisible: true });
  assert.deepEqual(plain(D.peakVisibility(both, false)), { visible: [false, false], axisVisible: false });
  const mixed = [{ msVisible: true, hasPeakData: true }, { msVisible: false, hasPeakData: true }, { msVisible: true, hasPeakData: false }];
  assert.deepEqual(plain(D.peakVisibility(mixed, true)), { visible: [true, false, false], axisVisible: true });
  const none = [{ msVisible: false, hasPeakData: true }];
  assert.deepEqual(plain(D.peakVisibility(none, true)), { visible: [false], axisVisible: false });
  assert.deepEqual(plain(D.peakVisibility([], true)), { visible: [], axisVisible: false });
});

function peakChart(showPeaks) {
  const visibility = new Map();
  const datasets = [
    { _kind: "ms", _srcKey: "tcp:25", data: [{ y: 8 }] },
    { _kind: "peak", _srcKey: "tcp:25", data: [{ y: 17 }] },
    { _kind: "ms", _srcKey: "tcp:587", data: [{ y: 9 }] },
    { _kind: "peak", _srcKey: "tcp:587", data: [{ y: 21 }] },
  ];
  visibility.set(2, false);
  visibility.set(3, false);
  const scales = { x: { min: 100, max: 200 } };
  return {
    visibility,
    chart: {
      data: { datasets }, scales, _showPeaks: showPeaks,
      options: { scales: { yPeak: { display: false } } },
      isDatasetVisible: (index) => visibility.get(index) ?? true,
      setDatasetVisibility: (index, visible) => visibility.set(index, visible),
    },
  };
}

test("showing peaks follows the legend state of each series and keeps the zoom range", () => {
  const context = loadChart();
  const { chart, visibility } = peakChart(true);
  context._syncPeaks(chart);
  assert.equal(visibility.get(1), true);
  assert.equal(visibility.get(3), false, "peak of a hidden series stays hidden");
  assert.equal(chart.options.scales.yPeak.display, true);
  visibility.set(2, true);
  context._syncPeaks(chart);
  assert.equal(visibility.get(3), true, "revealing the series reveals its peak");
  chart._showPeaks = false;
  context._syncPeaks(chart);
  assert.deepEqual([visibility.get(1), visibility.get(3)], [false, false]);
  assert.equal(chart.options.scales.yPeak.display, false, "no empty right axis while peaks are off");
  assert.deepEqual(plain(chart.scales.x), { min: 100, max: 200 });
});

test("legend lists one entry per series and no peak entries; a toggle updates the peaks", () => {
  const makeElement = () => ({
    children: [], attributes: {}, style: { setProperty() {} }, listeners: {},
    classList: { add() {} },
    appendChild(child) { this.children.push(child); },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    focus() {},
    set textContent(value) { this._text = value; this.children = []; },
    get textContent() { return this._text || this.children.map((child) => child.textContent).join(""); },
  });
  const context = loadChart({ document: { createElement: makeElement } });
  const { chart, visibility } = peakChart(true);
  Object.assign(chart, {
    _palette: { series: ["#111111", "#222222", "#333333"], down: "#444444" },
    _bandCount: 0, update() {}, _renderText() {},
  });
  chart.data.datasets.forEach((d, i) => { d._slot = i < 2 ? 2 : 3; d.label = d._kind + i; d._srcId = "tcp"; });
  const legend = makeElement();
  context._renderCombinedLegend(chart, legend, makeElement());
  assert.deepEqual(legend.children.map((b) => b.attributes["aria-label"]), ["TCP 25", "TCP 587"]);
  assert.ok(legend.children.every((b) => !/peak/i.test(b.attributes["aria-label"])));
  legend.children[1].listeners.click();
  assert.equal(visibility.get(2), true);
  assert.equal(visibility.get(3), true, "showing TCP 587 shows its peak while peaks are on");
});
