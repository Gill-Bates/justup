//
// tests/js/availability-chart-clip.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const read = (path) => readFileSync(new URL(`../../${path}`, import.meta.url), "utf8");

// Runs buildAvailabilitySeriesChart with a fake Chart and returns the config it hands over.
function buildConfig(seriesData) {
  const els = {};
  const element = () => ({ hidden: false, textContent: "", setAttribute() {}, querySelector: () => null, dataset: {}, addEventListener() {}, removeEventListener() {}, clientWidth: 900, parentElement: { clientWidth: 900 } });
  let captured = null;
  const context = {
    window: { matchMedia: () => ({ matches: false }), innerWidth: 1280 },
    navigator: { language: "en-GB" },
    document: { getElementById: (id) => (els[id] ||= element()), createElement: element },
    appState: { charts: new Map(), theme: { mode: "light", text: "#000", muted: "#666", grid: "#ccc" } },
    console,
    setTimeout: () => 0,
    withAlpha: (c) => c,
    getCssVar: (name, fallback) => fallback,
    updateChartSampling() {},
    _getVisibleRangeMs() { return 0; },
    Chart: class { constructor(canvas, config) { captured = config; this.options = config.options; this.data = config.data; this.canvas = canvas; } destroy() {} },
  };
  vm.runInNewContext(read("app/static/js/charts/chart-config.js"), context);
  vm.runInNewContext(read("app/static/js/charts/chart-reset.js"), context);
  vm.runInNewContext(read("app/static/js/charts/chart-builder.js"), context);
  context.buildAvailabilitySeriesChart("chart-uptime", { series: seriesData });
  return captured;
}

const pt = (minute, up) => ({ ts: new Date(Date.UTC(2026, 0, 1, 0, minute)).toISOString(), up });

test("every availability series is clipped to the plot area, one series or several", () => {
  for (const data of [
    { HTTP: [pt(0, 1), pt(10, 0), pt(20, 1)] },
    { HTTP: [pt(0, 1), pt(10, 1)], PING: [pt(0, 1), pt(10, 0)], "TCP:443": [pt(0, 1)], "UDP:53": [pt(5, 1)] },
  ]) {
    const config = buildConfig(data);
    assert.ok(config.data.datasets.length >= 1);
    for (const dataset of config.data.datasets) {
      assert.equal(dataset.clip, 0, `${dataset.label}: clip must be 0 (exactly the chart area), not off`);
      assert.equal(dataset.stepped, "before", `${dataset.label}: step lines are what the clip is for`);
    }
  }
});

test("the UDP dash is the one dashed series and its legend key repeats it (DC-EX-CHART-1)", () => {
  const config = buildConfig({ HTTP: [pt(0, 1), pt(10, 1)], "TCP:443": [pt(0, 1)], "UDP:53": [pt(5, 1)] });
  const dashed = config.data.datasets.filter((dataset) => (dataset.borderDash || []).length > 0).map((dataset) => dataset.label);
  assert.deepEqual([...dashed], ["UDP:53"]);
  const chart = { data: config.data, getDatasetMeta: () => ({ hidden: false }), options: { plugins: {} } };
  const keys = config.options.plugins.legend.labels.generateLabels(chart);
  assert.deepEqual([...keys.map((key) => key.lineDash.length > 0)], [...config.data.datasets.map((dataset) => (dataset.borderDash || []).length > 0)]);
});
