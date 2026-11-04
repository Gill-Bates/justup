//
// tests/js/chart-reset.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/charts/chart-reset.js", import.meta.url), "utf8");
const sandbox = {};
sandbox.window = sandbox;
vm.runInNewContext(src, sandbox);
const { resetChartView, captureLoadView } = sandbox.JuChartReset;
const plain = (value) => JSON.parse(JSON.stringify(value));

// A chart double with the surface the reset touches: scale options, datasets and the zoom plugin.
function fakeChart() {
  const loaded = [[1, 2, 3], [4, 5]];
  const chart = {
    calls: [],
    options: { scales: { x: { min: 100, max: 200 }, y: {}, yPeak: { beginAtZero: true }, yStatus: { min: 0, max: 1 } } },
    data: { datasets: loaded.map((data) => ({ data: data.slice() })) },
    resetZoom() { this.calls.push("resetZoom"); },
    update(mode) { this.calls.push("update:" + mode); },
    _cancelZoomLoader() { this.calls.push("cancel"); },
    _restoreLoadView() { this.calls.push("restore"); this.data.datasets.forEach((d, i) => { d.data = loaded[i].slice(); }); },
  };
  captureLoadView(chart);
  return chart;
}
const zoom = (chart) => {
  Object.assign(chart.options.scales.x, { min: 130, max: 150 });
  Object.assign(chart.options.scales.y, { min: 0, max: 1 });
  Object.assign(chart.options.scales.yPeak, { min: 0, max: 500 });
  chart.options.scales.yStatus.max = 9;
  chart.data.datasets[0].data = [9];
};
const state = (chart) => plain({ scales: chart.options.scales, data: chart.data.datasets.map((d) => d.data) });

test("reset returns every scale and every series to the load state", () => {
  const chart = fakeChart();
  const before = state(chart);
  zoom(chart);
  resetChartView(chart);
  assert.deepEqual(state(chart), before);
  assert.ok(!("min" in chart.options.scales.y) && !("max" in chart.options.scales.yPeak), "auto scales stay auto");
});

test("reset is idempotent and loses neither series nor points", () => {
  const chart = fakeChart();
  zoom(chart);
  resetChartView(chart);
  const once = state(chart);
  resetChartView(chart);
  resetChartView(chart);
  assert.deepEqual(state(chart), once);
  assert.deepEqual(once.data.map((d) => d.length), [3, 2]);
});

test("a pending detail request is cancelled before the view is restored", () => {
  const chart = fakeChart();
  zoom(chart);
  resetChartView(chart);
  assert.ok(chart.calls.indexOf("cancel") < chart.calls.indexOf("restore"));
  assert.ok(chart.calls.includes("update:none"));
});

test("a chart without a restore hook (plain line chart) falls back to its reload hook", () => {
  const chart = fakeChart();
  delete chart._restoreLoadView;
  chart._reloadVisible = () => chart.calls.push("reload");
  zoom(chart);
  resetChartView(chart);
  assert.ok(chart.calls.includes("reload"));
  assert.equal(chart.options.scales.x.min, 100);
});

test("a chart that was never captured is left alone instead of throwing", () => {
  assert.doesNotThrow(() => resetChartView({ options: { scales: {} }, update() {} }));
  assert.doesNotThrow(() => resetChartView(null));
});
