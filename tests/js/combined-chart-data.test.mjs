//
// tests/js/combined-chart-data.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/charts/combined-chart-data.js", import.meta.url), "utf8");
const sandbox = { window: {} };
vm.runInNewContext(src, sandbox);
const D = sandbox.window.CombinedChartData;
// Arrays from the vm realm have another prototype; compare them as JSON.
const plain = (v) => JSON.parse(JSON.stringify(v));
const T0 = Date.parse("2026-01-01T00:00:00Z");
const pt = (min, value) => ({ ts: new Date(T0 + min * 60000).toISOString(), value });
function controlledTimers() {
  let nextId = 0;
  const pending = new Map();
  return {
    setTimeout(fn, ms) { const id = ++nextId; pending.set(id, { fn, ms }); return id; },
    clearTimeout(id) { pending.delete(id); },
    async run(ms) {
      const entry = [...pending].find(([, timer]) => timer.ms === ms);
      assert.ok(entry, `expected a pending ${ms} ms timer`);
      pending.delete(entry[0]);
      await entry[1].fn();
    },
  };
}

test("status codes are classified by class with a distinct shape each", () => {
  assert.equal(D.classifyStatus(200).key, "2xx");
  assert.equal(D.classifyStatus(301).key, "3xx");
  assert.equal(D.classifyStatus(404).key, "4xx");
  assert.equal(D.classifyStatus(503).key, "5xx");
  assert.equal(D.classifyStatus(0).key, "err");
  assert.equal(D.classifyStatus(999).key, "err");
  assert.equal(D.classifyStatus(null).key, "err");
  const shapes = new Set(Object.values(D.STATUS_CLASSES).map((c) => c.shape));
  assert.equal(shapes.size, Object.keys(D.STATUS_CLASSES).length);
});

test("default latency source follows HTTP > Ping > TCP", () => {
  const all = D.enabledSources({ enableHttp: true, enablePing: true, enableTcp: true });
  assert.deepEqual(plain(all.map((s) => s.id)), ["http", "ping", "tcp"]);
  assert.equal(D.enabledSources({ enablePing: true, enableTcp: true })[0].id, "ping");
  assert.equal(D.enabledSources({}).length, 0);
});

test("gaps in the series become null points instead of interpolated lines", () => {
  const series = D.toGappedSeries([pt(0, 10), pt(1, 12), pt(2, 11), pt(60, 13)]);
  assert.equal(series.filter((p) => p.y === null).length, 1);
  assert.equal(series.length, 5);
  assert.equal(D.toGappedSeries([pt(0, 10), pt(1, 12), pt(2, 11)]).length, 3);
});

test("peak response times preserve rollup gaps and match measurement gaps without changing peak values", () => {
  const points = [
    { ts: new Date(T0).toISOString(), value: 100, max: 350, count: 6 },
    { ts: new Date(T0 + 60000).toISOString(), value: 150, max: 180, count: 5 },
    { ts: new Date(T0 + 120000).toISOString(), value: 200, max: null, count: 1, rollup: true },
    { ts: new Date(T0 + 180000).toISOString(), value: 250, max: 700, count: 4 },
    { ts: new Date(T0 + 4 * 60 * 60000).toISOString(), value: 300, max: 900, count: 3 },
  ];
  const minGapMs = 3 * 60 * 60000;
  const measurements = D.toGappedSeries(points, minGapMs);
  const peaks = D.toPeakSeries(points, minGapMs);
  assert.deepEqual(plain(peaks.map(p => p.y)), [350, 180, null, 700, null, 900]);
  assert.deepEqual(plain(peaks.filter(p => p.y !== null).map(p => [p.y, p.median, p.outlier, p.count])), [
    [350, 100, true, 6], [180, 150, false, 5], [700, 250, true, 4], [900, 300, true, 3],
  ]);
  const gapX = T0 + 180000 + 1;
  assert.equal(measurements.find(p => p.x === gapX).y, null);
  assert.equal(peaks.find(p => p.x === gapX).y, null);
  assert.equal(peaks[0].median, 100);
  assert.equal(peaks[0].count, 6);
});

test("status bucketing keeps the worst code of a bucket", () => {
  const points = [pt(0, 200), pt(1, 503), pt(2, 200), pt(500, 404)];
  const buckets = D.bucketStatus(points, T0, T0 + 600 * 60000, 2);
  assert.equal(buckets.length, 2);
  assert.equal(buckets[0].code, 503);
  assert.equal(buckets[0].count, 3);
  assert.equal(buckets[1].cls, "4xx");
  assert.deepEqual(plain(D.bucketStatus([], T0, T0 + 1, 5)), []);
});

test("down bands span from first down point to the next up point", () => {
  const bands = D.buildDownBands([pt(0, 1), pt(1, 0), pt(2, 0), pt(3, 1), pt(4, 0)]);
  assert.equal(bands.length, 2);
  assert.equal(bands[0].start, T0 + 60000);
  assert.equal(bands[0].end, T0 + 3 * 60000);
  assert.ok(bands[1].end > bands[1].start);
  assert.deepEqual(plain(D.buildDownBands([pt(0, 1), pt(1, 1)])), []);
});

test("summary text reports latency range, last status and outages", () => {
  const ms = D.toGappedSeries([pt(0, 100), pt(1, 200), pt(2, 300)]);
  const s = D.summarize(ms, [{ code: 200, cls: "2xx", x: 1, count: 1 }, { code: 503, cls: "5xx", x: 2, count: 1 }], [{}]);
  assert.equal(s.min, 100);
  assert.equal(s.median, 200);
  assert.equal(s.max, 300);
  assert.equal(s.lastCode, 503);
  const text = D.describeSummary("HTTP response time", s);
  assert.match(text, /min 100 ms, median 200 ms, max 300 ms/);
  assert.match(text, /last HTTP status 503/);
  assert.match(text, /1 outage$/);
  assert.match(D.describeSummary("Ping", D.summarize([], [], [])), /No response time data/);
});

test("visible summary and canvas aria label use the measured peak instead of the median ceiling", () => {
  const chartSource = readFileSync(new URL("../../app/static/js/charts/combined-chart.js", import.meta.url), "utf8");
  const elements = {};
  const makeElement = () => ({
    children: [], _text: "", get textContent() { return this._text; },
    set textContent(value) { this._text = value; this.children = []; },
    appendChild(child) { this.children.push(child); }, setAttribute(name, value) { this[name] = value; },
  });
  const document = { getElementById: id => (elements[id] ||= makeElement()), createElement: () => makeElement() };
  const attrs = {};
  const canvas = { setAttribute: (name, value) => { attrs[name] = value; } };
  const context = { window: {}, document, CombinedChartData: D, console };
  vm.runInNewContext(chartSource, context);
  const points = [
    { ts: new Date(T0).toISOString(), value: 100, max: 3200, min: 80, count: 60 },
    { ts: new Date(T0 + 60000).toISOString(), value: 120, max: 180, min: 100, count: 60 },
  ];
  const series = D.toGappedSeries(points);
  const summary = context._summarizeSource(series, points);
  const view = { summaries: { http: summary }, series: { http: series }, statusBuckets: [], bands: [], zoomed: false };
  const input = { sources: [{ key: "http", label: "HTTP response time" }], range: { from: new Date(T0), to: new Date(T0 + 60000) } };
  context._renderCombinedText(canvas, input, view);
  assert.match(elements["combined-summary"].textContent, /max 3200 ms/);
  assert.match(attrs["aria-label"], /max 3200 ms/);
});

test("combined legend accessible names match visible labels and titles retain detail", () => {
  const chartSource = readFileSync(new URL("../../app/static/js/charts/combined-chart.js", import.meta.url), "utf8");
  const makeElement = () => ({
    children: [], attributes: {}, style: { setProperty() {} },
    appendChild(child) { this.children.push(child); },
    setAttribute(name, value) { this.attributes[name] = value; },
    listeners: {},
    addEventListener(name, callback) { this.listeners[name] = callback; },
    focus() {},
    set textContent(value) { this._text = value; this.children = []; },
    get textContent() { return this._text || this.children.map((child) => child.textContent).join(""); },
  });
  const context = {
    window: {}, document: { createElement: makeElement }, CombinedChartData: D, console,
  };
  vm.runInNewContext(chartSource, context);
  const datasets = [
    { label: "HTTP response time", _kind: "ms", _srcId: "http", _srcKey: "http", _slot: 0, data: [] },
    { label: "HTTP response time peak response time", _kind: "peak", _srcId: "http", _srcKey: "http", _slot: 0, data: [{ y: 120 }] },
    { label: "TCP connect :443", _kind: "ms", _srcId: "tcp", _srcKey: "tcp:443", _slot: 2, data: [] },
    { label: "HTTP status", _kind: "status", data: [] },
  ];
  const visibility = new Map();
  let textRenders = 0;
  const chart = {
    data: { datasets }, _palette: { series: ["#123456", "#222222", "#654321"], down: "#c0283a" },
    _bandCount: 1, _bandsVisible: true, _bandSourceLabel: "HTTP", _presentClasses: [],
    options: { plugins: { annotation: { annotations: { down0: {} } } } },
    isDatasetVisible: index => visibility.get(index) ?? true,
    setDatasetVisibility(index, visible) { visibility.set(index, visible); },
    update() {}, _renderText() { textRenders += 1; },
  };
  const legend = makeElement();
  context._renderCombinedLegend(chart, legend, makeElement());
  assert.deepEqual(legend.children.map((button) => button.children.at(-1).textContent), [
    "HTTP", "TCP 443", "Status", "Down (HTTP)",
  ]);
  assert.deepEqual(legend.children.map((button) => button.attributes["aria-label"]), [
    "HTTP", "TCP 443", "Status", "Down (HTTP)",
  ]);
  assert.deepEqual(legend.children.map((button) => button.title), [
    "HTTP response time", "TCP connect :443", "HTTP status", "Downtime (HTTP)",
  ]);
  assert.equal(datasets[0].label, "HTTP response time");
  legend.children[0].listeners.click();
  assert.equal(visibility.get(0), false);
  assert.equal(textRenders, 1);
  assert.equal(legend.children[0].attributes["aria-pressed"], "false");
});

test("combined summary and canvas name follow visible data layers", () => {
  const chartSource = readFileSync(new URL("../../app/static/js/charts/combined-chart.js", import.meta.url), "utf8");
  const elements = {};
  const document = {
    getElementById: id => (elements[id] ||= { textContent: "" }),
    createElement: () => ({ setAttribute() {}, appendChild() {} }),
  };
  const attrs = {};
  const canvas = { setAttribute: (name, value) => { attrs[name] = value; } };
  const context = { window: {}, document, CombinedChartData: D, console };
  vm.runInNewContext(chartSource, context);
  const http = D.toGappedSeries([pt(0, 100), pt(1, 200)]);
  const ping = D.toGappedSeries([pt(0, 10), pt(1, 20)]);
  const input = {
    sources: [
      { key: "http", label: "HTTP response time" },
      { key: "ping", label: "Ping" },
    ],
    statusEnabled: true,
    range: { from: new Date(T0), to: new Date(T0 + 60000) },
  };
  const view = {
    summaries: { http: context._summarizeSource(http, []), ping: context._summarizeSource(ping, []) },
    series: { http, ping },
    statusBuckets: [{ code: 503, cls: "5xx", x: T0, count: 1 }],
    bands: [{ start: T0, end: T0 + 60000 }],
    zoomed: false,
  };
  const chart = {
    data: { datasets: [
      { _kind: "ms", _srcKey: "http" },
      { _kind: "ms", _srcKey: "ping" },
      { _kind: "status" },
    ] },
    isDatasetVisible: index => index === 1,
    _bandsVisible: false,
    _bandCount: 1,
  };
  context._renderCombinedText(canvas, input, view, chart);
  const summary = elements["combined-summary"].textContent;
  assert.match(summary, /^Ping: min 10 ms, median 15 ms, max 20 ms/);
  assert.match(summary, /HTTP status layer hidden/);
  assert.match(summary, /downtime bands hidden/);
  assert.doesNotMatch(summary, /last HTTP status 503|1 outage/);
  assert.match(attrs["aria-label"], /Ping: min 10 ms/);
});

test("status hover is limited to the status strip's vertical bounds", () => {
  const chartSource = readFileSync(new URL("../../app/static/js/charts/combined-chart.js", import.meta.url), "utf8");
  let pointerY = 50;
  const statusElement = {};
  const chart = {
    data: { datasets: [{ _kind: "status", data: [{ x: 0, end: 100 }] }] },
    scales: {
      x: { getValueForPixel: () => 50 },
      yStatus: { top: 80, bottom: 100 },
    },
    isDatasetVisible: () => true,
    getDatasetMeta: () => ({ data: [statusElement] }),
  };
  const context = {
    window: {
      Chart: {
        Interaction: { modes: { point: () => [] } },
        helpers: { getRelativePosition: () => ({ x: 50, y: pointerY }) },
      },
    },
    CombinedChartData: D,
  };
  context.Chart = context.window.Chart;
  vm.runInNewContext(chartSource, context);
  context._registerCombinedInteraction();
  const mode = context.window.Chart.Interaction.modes.combinedStatus;
  assert.deepEqual(plain(mode(chart, { type: "mousemove" }, {}, false)), []);
  pointerY = 90;
  assert.equal(mode(chart, { type: "mousemove" }, {}, false)[0].element, statusElement);
});

test("status buckets older than the rollup cutoff are flagged as rollups", () => {
  const buckets = D.bucketStatus([pt(0, 351.5), pt(500, 200)], T0, T0 + 600 * 60000, 2, T0 + 100 * 60000);
  assert.equal(buckets[0].rollup, true);
  assert.equal(buckets[1].rollup, false);
});

test("server status buckets keep one 503 with exact counts and a hover interval", () => {
  const start = new Date(T0).toISOString();
  const end = new Date(T0 + 30 * 60000).toISOString();
  const buckets = D.bucketStatus([
    { ts: start, bucket_end: end, value: 503, count: 101, code_counts: { "200": 100, "503": 1 } },
  ], T0, T0 + 60000, 1);
  assert.equal(buckets.length, 1);
  assert.equal(buckets[0].cls, "5xx");
  assert.equal(buckets[0].count, 101);
  assert.equal(buckets[0].end, T0 + 30 * 60000);
  assert.equal(buckets[0].codeCounts["503"], 1);
});

test("display bucket gap does not invent zero latency or a continuous outage", () => {
  const points = [pt(0, 100), pt(1, 120), pt(60, 130)];
  const series = D.toGappedSeries(points, 3 * 60000);
  assert.equal(series.filter(p => p.y === null).length, 1);
  const bands = D.buildDownBands([pt(0, 0), pt(1, 0), pt(60, 0)], 60000);
  assert.equal(bands.length, 2);
  assert.equal(bands[0].end, T0 + 2 * 60000);
});

test("zoom reloads finer buckets once and reset restores the overview", async () => {
  const chartSource = readFileSync(new URL("../../app/static/js/charts/combined-chart.js", import.meta.url), "utf8");
  const calls = [];
  const timers = controlledTimers();
  const context = {
    window: {}, appState: { charts: new Map() }, pageConfig: { targetId: 7 },
    CombinedChartData: D, AbortController, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout, console,
    withAlpha: color => color,
    fetchDisplayMetricRange: async (_id, metric) => {
      calls.push(metric);
      if (metric === "http_ms") return {
        bucket_seconds: 3600, points: [
          { ...pt(60, 120), max: 500, min: 80, count: 4 },
          { ...pt(360, 130), max: 180, min: 100, count: 3 },
        ]
      };
      if (metric === "http_status") return {
        bucket_seconds: 60, points: [
          { ts: pt(60, 503).ts, bucket_end: pt(61, 503).ts, value: 503, count: 2, code_counts: { "200": 1, "503": 1 } },
        ]
      };
      return { bucket_seconds: 60, points: [pt(60, 1), pt(61, 0), pt(62, 1)] };
    },
  };
  vm.runInNewContext(chartSource, context);
  const original = [{ x: T0, y: 100 }];
  const originalPeaks = D.toPeakSeries([
    { ...pt(0, 100), max: 250, count: 1 },
    { ...pt(300, 120), max: 400, count: 1 },
  ], 3 * 60 * 60000);
  assert.deepEqual(plain(originalPeaks.map(p => p.y)), [250, null, 400]);
  const datasets = [
    { _kind: "ms", _srcKey: "http", data: original },
    { _kind: "peak", _srcKey: "http", data: originalPeaks },
    { _kind: "status", data: [{ x: T0, code: 200 }] },
  ];
  const chartOptions = {
    plugins: { annotation: { annotations: { down0: { xMin: T0, xMax: T0 + 1 } } } },
    scales: { yPeak: { display: false } },
  };
  const chart = {
    data: { datasets }, scales: { x: { min: T0, max: T0 + 7 * 86400000 } },
    options: chartOptions, config: { options: chartOptions },
    chartArea: { width: 900 }, canvas: { clientWidth: 900 }, _bandCount: 1, _palette: { down: "#f00" },
    update() { }, _renderLegend() { }, _showPeaks: true,
    isDatasetVisible: () => true, setDatasetVisibility() { },
  };
  context.appState.charts.set("chart", chart);
  chart._reloadVisible = context._createCombinedZoomLoader(chart, "chart", {
    sources: [{ id: "http", key: "http" }], statusEnabled: true, upSourceLabel: "HTTP",
  }, datasets, T0, T0 + 7 * 86400000);
  chart.scales.x.min = T0 + 60 * 60000;
  chart.scales.x.max = T0 + 6 * 60 * 60000;
  chart._reloadVisible();
  chart._reloadVisible();
  await timers.run(300);
  assert.deepEqual(calls, ["http_ms", "http_status", "http_up"]);
  assert.equal(chart.data.datasets[0].data.length, 3);
  assert.deepEqual(plain(chart.data.datasets[1].data.map(p => p.y)), [500, null, 180]);
  assert.equal(chart.data.datasets[2].data[0].code, 503);
  assert.equal(chart.options.scales.yPeak.display, true);
  chart._palette.down = "#0f0";
  chart.scales.x.min = T0;
  chart.scales.x.max = T0 + 7 * 86400000;
  chart._reloadVisible();
  assert.equal(chart.data.datasets[0].data, original);
  assert.equal(chart.data.datasets[1].data, originalPeaks);
  assert.equal(chart.options.plugins.annotation.annotations.down0.backgroundColor, "#0f0");
  assert.equal(chart.options.plugins.annotation.annotations.down0.borderColor, "#0f0");
  assert.equal(calls.length, 3);
});

test("starting a chart build cancels its pending zoom loader and advances the generation", () => {
  const chartSource = readFileSync(new URL("../../app/static/js/charts/combined-chart.js", import.meta.url), "utf8");
  const context = { window: {}, appState: { charts: new Map() }, CombinedChartData: D, console };
  vm.runInNewContext(chartSource, context);
  let cancelled = false;
  context.appState.charts.set("chart", {
    _buildGeneration: 4,
    _cancelZoomLoader() { cancelled = true; },
  });
  assert.equal(context._beginCombinedChartBuild("chart"), 5);
  assert.equal(cancelled, true);
});

test("a delayed zoom response cannot overwrite data after the chart build generation changes", async () => {
  const chartSource = readFileSync(new URL("../../app/static/js/charts/combined-chart.js", import.meta.url), "utf8");
  const timers = controlledTimers();
  const pending = [];
  const context = {
    window: {}, appState: { charts: new Map() }, pageConfig: { targetId: 7 },
    CombinedChartData: D, AbortController, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    console, withAlpha: color => color,
    fetchDisplayMetricRange: () => new Promise(resolve => pending.push(resolve)),
  };
  vm.runInNewContext(chartSource, context);
  const original = [{ x: T0, y: 100 }];
  const datasets = [
    { _kind: "ms", _srcKey: "http", data: original },
    { _kind: "status", data: [] },
  ];
  const chartOptions = { plugins: { annotation: { annotations: { down0: { xMin: T0, xMax: T0 + 1 } } } }, scales: {} };
  const chart = {
    data: { datasets }, config: { options: chartOptions }, options: chartOptions,
    scales: { x: { min: T0 + 60000, max: T0 + 120000 } },
    chartArea: { width: 900 }, canvas: { clientWidth: 900 },
    _buildGeneration: 1, _bandCount: 1, _bandsVisible: true, _palette: { down: "#f00" },
    update() {}, _renderLegend() {},
  };
  context.appState.charts.set("chart", chart);
  chart._reloadVisible = context._createCombinedZoomLoader(
    chart,
    "chart",
    { sources: [{ id: "http", key: "http" }], statusEnabled: true, upSourceLabel: "HTTP" },
    datasets,
    T0,
    T0 + 7 * 86400000,
    undefined,
    chart._buildGeneration,
  );
  chart._reloadVisible();
  const loading = timers.run(300);
  await Promise.resolve();
  assert.equal(pending.length, 3);
  chart._buildGeneration += 1;
  const replacement = [{ x: T0 + 60000, y: 900 }];
  chart.data.datasets[0].data = replacement;
  chart.config.options.plugins.annotation.annotations = { downB: { xMin: T0, xMax: T0 + 1 } };
  pending.forEach(resolve => resolve({ bucket_seconds: 60, points: [pt(60, 200)] }));
  await loading;
  assert.equal(chart.data.datasets[0].data, replacement);
  assert.deepEqual(Object.keys(chart.config.options.plugins.annotation.annotations), ["downB"]);
});

test("zoom request failures are announced in the combined chart status note", async () => {
  const chartSource = readFileSync(new URL("../../app/static/js/charts/combined-chart.js", import.meta.url), "utf8");
  const timers = controlledTimers();
  const note = { textContent: "Base status.", attributes: {}, setAttribute(name, value) { this.attributes[name] = value; } };
  const context = {
    window: {}, document: { getElementById: () => note }, appState: { charts: new Map() },
    pageConfig: { targetId: 7 }, CombinedChartData: D, AbortController,
    setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    console: { warn() {} }, withAlpha: color => color,
    fetchDisplayMetricRange: async () => { throw new Error("offline"); },
  };
  vm.runInNewContext(chartSource, context);
  const datasets = [{ _kind: "ms", _srcKey: "http", data: [{ x: T0, y: 100 }] }];
  const chartOptions = { plugins: { annotation: { annotations: {} } }, scales: {} };
  const chart = {
    data: { datasets }, config: { options: chartOptions }, options: chartOptions,
    scales: { x: { min: T0 + 60000, max: T0 + 120000 } },
    chartArea: { width: 900 }, canvas: { clientWidth: 900 },
    _buildGeneration: 1, _statusNoteBase: "Base status.", _bandCount: 0, _bandsVisible: true,
    _palette: { down: "#f00" }, update() {}, _renderLegend() {},
  };
  context.appState.charts.set("chart", chart);
  chart._reloadVisible = context._createCombinedZoomLoader(
    chart, "chart", { sources: [{ id: "http", key: "http" }], statusEnabled: true, upSourceLabel: "HTTP" },
    datasets, T0, T0 + 7 * 86400000, undefined, chart._buildGeneration,
  );
  chart._reloadVisible();
  await timers.run(300);
  assert.match(note.textContent, /Zoom details could not be loaded/);
  assert.equal(note.attributes.role, "status");
  assert.equal(note.attributes["aria-live"], "polite");
});

test("zoom reset is safe when the initial chart has no peak axis", () => {
  const chartSource = readFileSync(new URL("../../app/static/js/charts/combined-chart.js", import.meta.url), "utf8");
  const context = { window: {}, appState: { charts: new Map() }, CombinedChartData: D, AbortController, setTimeout, clearTimeout, console };
  vm.runInNewContext(chartSource, context);
  const data = [{ x: T0, y: 100 }];
  const datasets = [{ _kind: "ms", _srcKey: "http", data }];
  const chartOptions = { plugins: { annotation: { annotations: {} } }, scales: {} };
  const chart = {
    data: { datasets }, scales: { x: { min: T0, max: T0 + 7 * 86400000 } },
    options: chartOptions, config: { options: chartOptions },
    _bandCount: 0, update() { }, _renderLegend() { },
  };
  context.appState.charts.set("chart", chart);
  chart._reloadVisible = context._createCombinedZoomLoader(chart, "chart", { sources: [], statusEnabled: false }, datasets, T0, T0 + 7 * 86400000);
  assert.doesNotThrow(() => chart._reloadVisible());
  assert.equal(chart.data.datasets[0].data, data);
});

test("SLA thresholds exist only for HTTP and Ping and only when SLA is enabled", () => {
  const sla = { enabled: true, responseTimeMs: 500, pingLatencyMs: 100 };
  assert.equal(D.slaFor("ping", sla).label, "100 ms");
  assert.equal(D.slaFor("tcp", sla), null);
  assert.equal(D.slaFor("http", { ...sla, enabled: false }), null);
});

test("TCP with several ports yields one series per port, one port keeps tcp_ms", () => {
  const flags = { enableHttp: true, enableTcp: true };
  const res = { http: { points: [1] }, tcp: { points: [2] }, tcpPorts: [{ points: [1] }, { points: [] }] };
  const multi = D.buildLatencySources(flags, ["80", "443"], res, { enabled: true, responseTimeMs: 300 });
  assert.equal(plain(multi.map((s) => s.label)).join("|"), "HTTP response time|TCP connect :80|TCP connect :443");
  assert.equal(multi[0].sla.value, 300);
  assert.notEqual(multi[1].dash, multi[2].dash);
  const single = D.buildLatencySources(flags, ["443"], res, null);
  assert.equal(single.length, 2);
  assert.equal(single[1].key, "tcp");
  const fallback = D.buildLatencySources(flags, ["80", "443"], { ...res, tcpPorts: [{ points: [] }, { points: [] }] }, null);
  assert.equal(fallback[1].key, "tcp");
});

test("informational 1xx codes form their own non-success class", () => {
  for (const code of [100, 101, 199]) assert.equal(D.classifyStatus(code).key, "info");
  assert.equal(D.classifyStatus(200).key, "2xx");
  assert.equal(D.classifyStatus(99).key, "err");
  assert.equal(D.classifyStatus(undefined).key, "err");
  assert.equal(D.classifyStatus("").key, "err");
  assert.equal(D.classifyStatus("abc").key, "err");
  assert.ok(D.STATUS_CLASSES.info.rank > D.STATUS_CLASSES["2xx"].rank);
  assert.ok(D.STATUS_CLASSES.info.rank < D.STATUS_CLASSES["5xx"].rank);
  const buckets = D.bucketStatus([{ ts: 1000, value: 200 }, { ts: 2000, value: 101 }], 0, 10000, 1);
  assert.equal(buckets[0].cls, "info");
});

test("zoom reload refreshes summary and label; reset restores the overview", async () => {
  const chartSource = readFileSync(new URL("../../app/static/js/charts/combined-chart.js", import.meta.url), "utf8");
  const timers = controlledTimers();
  const els = {};
  const makeEl = () => ({
    children: [],
    _text: "",
    get textContent() { return this._text; },
    set textContent(v) { this._text = v; this.children = []; },
    appendChild(c) { this.children.push(c); },
  });
  const document = {
    getElementById: (id) => (els[id] ||= makeEl()),
    createElement: () => makeEl(),
  };
  const attrs = {};
  const canvas = { setAttribute: (k, v) => { attrs[k] = v; } };
  const context = {
    window: {}, document, appState: { charts: new Map() }, pageConfig: { targetId: 7 },
    CombinedChartData: D, AbortController, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout, console, withAlpha: (c) => c,
    fetchDisplayMetricRange: async (_id, metric) => {
      if (metric === "http_ms") return { bucket_seconds: 60, points: [pt(60, 900), pt(61, 1000)] };
      return { bucket_seconds: 60, points: [pt(60, 1), pt(61, 1)] };
    },
  };
  vm.runInNewContext(chartSource, context);
  const srcPoints = [pt(0, 100), pt(1, 200)];
  const input = {
    sources: [{ id: "http", key: "http", label: "HTTP response time", points: srcPoints }],
    statusEnabled: false, upSourceLabel: "HTTP", range: { from: new Date(T0), to: new Date(T0 + 7 * 86400000) },
  };
  const series = D.toGappedSeries(srcPoints);
  const fullView = {
    summaries: { http: context._summarizeSource(series, srcPoints) }, series: { http: series },
    statusBuckets: [], bands: [], zoomed: false
  };
  context._renderCombinedText(canvas, input, fullView);
  const overview = els["combined-summary"].textContent;
  assert.match(overview, /min 100 ms/);
  const datasets = [{ _kind: "ms", _srcKey: "http", data: series }];
  const chartOptions = { plugins: { annotation: { annotations: {} } }, scales: { yPeak: { display: false } } };
  const chart = {
    data: { datasets }, scales: { x: { min: T0 + 60 * 60000, max: T0 + 90 * 60000 } },
    options: chartOptions, config: { options: chartOptions },
    chartArea: { width: 900 }, canvas: { clientWidth: 900 }, _bandCount: 0, _palette: { down: "#f00" },
    update() { }, _renderLegend() { },
  };
  context.appState.charts.set("chart", chart);
  const onView = (view) => context._renderCombinedText(canvas, input, view || fullView);
  chart._reloadVisible = context._createCombinedZoomLoader(chart, "chart", input, datasets, T0, T0 + 7 * 86400000, onView);
  chart._reloadVisible();
  await timers.run(300);
  assert.match(els["combined-summary"].textContent, /min 900 ms/);
  assert.match(els["combined-summary"].textContent, /zoomed range/);
  assert.match(attrs["aria-label"], /min 900 ms/);
  chart.scales.x.min = T0;
  chart.scales.x.max = T0 + 7 * 86400000;
  chart._reloadVisible();
  assert.equal(els["combined-summary"].textContent, overview);
});

test("rebuilding a chart leaves exactly one dblclick listener on the canvas", () => {
  const src = readFileSync(new URL("../../app/static/js/charts/chart-builder.js", import.meta.url), "utf8");
  const listeners = new Set();
  const canvas = {
    dataset: {},
    addEventListener: (_t, fn) => listeners.add(fn),
    removeEventListener: (_t, fn) => listeners.delete(fn),
  };
  const context = { window: {}, document: {}, appState: { charts: new Map() }, setTimeout: () => 0 };
  vm.runInNewContext(src, context);
  for (let i = 0; i < 3; i += 1) {
    context._destroyExisting("c");
    const chart = { canvas, destroy() { }, options: {} };
    context._registerChart("c", chart, canvas, true);
  }
  assert.equal(listeners.size, 1);
});

test("refreshing a chart reuses its instance and preserves dataset visibility", () => {
  const src = readFileSync(new URL("../../app/static/js/charts/chart-builder.js", import.meta.url), "utf8");
  let constructions = 0;
  let destroys = 0;
  const context = {
    window: {}, document: {}, appState: { charts: new Map() }, setTimeout: () => 0,
    Chart: function (canvas, config) {
      constructions += 1;
      this.canvas = canvas;
      this.data = config.data;
      this.options = config.options;
      this.update = (mode) => { this.updateMode = mode; };
      this.isDatasetVisible = (index) => !this.data.datasets[index].hidden;
      this.setDatasetVisibility = (index, visible) => { this.data.datasets[index].hidden = !visible; };
      this.destroy = () => { destroys += 1; };
    },
  };
  vm.runInNewContext(src, context);
  const canvas = {
    dataset: {},
    addEventListener() { },
    removeEventListener() { },
  };
  const firstConfig = { data: { datasets: [{ _kind: "ms", _srcKey: "http", data: [1] }] }, options: {} };
  const first = context._createOrUpdateChart("chart", canvas, firstConfig);
  context._registerChart("chart", first, canvas, true);
  first.data.datasets[0].hidden = true;
  const nextConfig = { data: { datasets: [{ _kind: "ms", _srcKey: "http", data: [2] }] }, options: {} };
  const refreshed = context._createOrUpdateChart("chart", canvas, nextConfig);
  assert.equal(refreshed, first);
  assert.equal(constructions, 1);
  assert.equal(destroys, 0);
  assert.equal(refreshed.data.datasets[0].data[0], 2);
  assert.equal(refreshed.data.datasets[0].hidden, true);
  assert.equal(refreshed.updateMode, "none");
});

test("responsive legend follows chart width and updates when the chart resizes", () => {
  const src = readFileSync(new URL("../../app/static/js/charts/chart-builder.js", import.meta.url), "utf8");
  let fitDashboard = false;
  const context = {
    window: { innerWidth: 1366, matchMedia: () => ({ matches: fitDashboard }) },
    appState: { charts: new Map() }, setTimeout: () => 0,
  };
  vm.runInNewContext(src, context);
  const canvas = { dataset: {}, parentElement: { clientWidth: 640 }, addEventListener() {}, removeEventListener() {} };
  assert.equal(context._legendPosition(canvas), "bottom");

  const chart = { canvas, width: 640, options: { plugins: { legend: { position: "right" } } }, update() {} };
  context._registerChart("availability", chart, canvas, true);
  chart.options.onResize(chart, { width: 640, height: 300 });
  assert.equal(chart.options.plugins.legend.position, "bottom");
  chart.width = 900;
  chart.options.onResize(chart, { width: 900, height: 300 });
  assert.equal(chart.options.plugins.legend.position, "right");
  chart.width = 640;
  fitDashboard = true;
  chart.options.onResize(chart, { width: 640, height: 300 });
  assert.equal(chart.options.plugins.legend.position, "right");
});

test("latency source priority mirrors _PRIORITY in app/utils/uptime_metric.py", () => {
  const py = readFileSync(new URL("../../app/utils/uptime_metric.py", import.meta.url), "utf8");
  const block = py.match(/_PRIORITY\s*=\s*\(([^\n]*)\)\s*\n/);
  assert.ok(block, "_PRIORITY tuple not found");
  const pyMetrics = [...block[1].matchAll(/"(\w+_up)"\)/g)].map((m) => m[1]);
  assert.deepEqual(pyMetrics, plain(D.SOURCES.map((s) => s.upMetric)));
});

test("fallback status buckets carry explicit bounds like server buckets", () => {
  const W = 600 * 60000;
  const buckets = D.bucketStatus([pt(0, 200), pt(1, 503), pt(2, 200), pt(500, 404)], T0, T0 + W, 2);
  assert.equal(buckets.length, 2);
  assert.equal(buckets[0].x, T0);
  assert.equal(buckets[0].end, T0 + W / 2);
  assert.equal(buckets[1].x, T0 + W / 2);
  assert.equal(buckets[1].end, T0 + W);
  assert.deepEqual(plain(buckets[0].codeCounts), { "200": 2, "503": 1 });
  assert.equal(buckets[0].code, 503);
  buckets.forEach((b) => ["x", "end", "code", "cls", "count", "codeCounts", "rollup"].forEach((k) => assert.ok(k in b, k)));
});

test("fallback status buckets: single point, gaps and out-of-range points", () => {
  const W = 100 * 60000;
  const single = D.bucketStatus([pt(10, 200)], T0, T0 + W, 1);
  assert.equal(single.length, 1);
  assert.deepEqual([single[0].x, single[0].end], [T0, T0 + W]);
  const gap = D.bucketStatus([pt(0, 200), pt(99, 200)], T0, T0 + W, 10);
  assert.equal(gap.length, 2);
  assert.equal(gap[1].x, T0 + 0.9 * W);
  // A point exactly at the range end stays in the last bucket instead of creating an extra one.
  const edge = D.bucketStatus([pt(100, 200)], T0, T0 + W, 4);
  assert.equal(edge.length, 1);
  assert.equal(edge[0].end, T0 + W);
});
test("normalizeHttpStatusCode rounds and accepts only 100-599", () => {
  for (const [input, expected] of [[99, null], [99.4, null], [100, 100], [199, 199], [200, 200], [200.4, 200], [599, 599], [600, null], ["abc", null], [null, null], [undefined, null], [NaN, null], [Infinity, null]]) {
    assert.equal(D.normalizeHttpStatusCode(input), expected, String(input));
  }
});
