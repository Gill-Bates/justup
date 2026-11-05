//
// tests/js/combined-chart-zoom.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const read = (path) => readFileSync(new URL(`../../${path}`, import.meta.url), "utf8");
const dataSandbox = { window: {} };
vm.runInNewContext(read("app/static/js/charts/combined-chart-data.js"), dataSandbox);
const T0 = Date.parse("2026-01-01T00:00:00Z");
const pt = (min, value) => ({ ts: new Date(T0 + min * 60000).toISOString(), value });

async function zoom(statusEnabled) {
  const calls = [];
  const chart = {
    _buildGeneration: 1,
    _bandCount: 0,
    _bandsVisible: true,
    _palette: { down: "#f00" },
    chartArea: { width: 600 },
    scales: { x: { min: T0, max: T0 + 60 * 60000 } },
    data: { datasets: [] },
    config: { options: { plugins: { annotation: { annotations: {} } } } },
    options: { plugins: { annotation: { annotations: {} } } },
    isDatasetVisible: () => true,
    update() {},
    _renderLegend() {},
  };
  chart.config.options = chart.options;
  let timerFn;
  const context = {
    window: {}, CombinedChartData: dataSandbox.window.CombinedChartData, console,
    AbortController, clearTimeout() {},
    setTimeout: (fn) => { timerFn = fn; return 1; },
    appState: { charts: new Map([["c", chart]]) },
    withAlpha: (c) => c,
    fetchDisplayMetricRange: async (_id, metric) => {
      calls.push(metric);
      if (metric === "ping_up") return { bucket_seconds: 60, points: [pt(0, 1), pt(1, 0), pt(2, 0), pt(3, 1)] };
      return { bucket_seconds: 60, points: [pt(0, 200)] };
    },
    pageConfig: { targetId: 5 },
  };
  vm.runInNewContext(read("app/static/js/charts/combined-chart.js"), context);
  const input = {
    sources: [{ key: "ping", id: "ping" }], statusEnabled, upSourceLabel: "Ping",
  };
  const load = vm.runInContext("_createCombinedZoomLoader", context)(
    chart, "c", input, [], T0 - 3600000, T0 + 7200000, null, 1,
  );
  load();
  await timerFn();
  return { calls, chart };
}

test("zoom without HTTP status requests no http_status and still builds down bands from _up", async () => {
  const { calls, chart } = await zoom(false);
  assert.deepEqual(calls, ["ping_ms", "ping_up"]);
  assert.equal(chart._bandCount, 1);
});

function summaryText(rawRetentionDays, ageDays) {
  const summary = { textContent: "" };
  const canvas = { setAttribute() {} };
  const context = {
    window: {}, CombinedChartData: dataSandbox.window.CombinedChartData, console,
    document: { getElementById: (id) => (id === "combined-summary" ? summary : null) },
    pageConfig: { rawRetentionDays },
  };
  vm.runInNewContext(read("app/static/js/charts/combined-chart.js"), context);
  const input = {
    sources: [], statusEnabled: false,
    range: { from: new Date(Date.now() - ageDays * 86400000), to: new Date() },
  };
  const view = { summaries: {}, series: {}, statusBuckets: [], bands: [] };
  vm.runInContext("_renderCombinedText", context)(canvas, input, view, null);
  return summary.textContent;
}

test("rollup note follows the server-provided raw retention window", () => {
  assert.doesNotMatch(summaryText(30, 29), /uses stored rollups/);
  assert.match(summaryText(30, 31), /older than 30 days uses stored rollups/);
  assert.match(summaryText(14, 20), /older than 14 days uses stored rollups/);
});
