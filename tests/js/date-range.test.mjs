//
// tests/js/date-range.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/target-detail/date-range.js", import.meta.url), "utf8");

/** Load the script with a frozen "now"; returns its global functions. */
function load(nowIso, useUtc, extra = {}) {
  const RealDate = Date;
  const fixed = new RealDate(nowIso).getTime();
  class FixedDate extends RealDate {
    constructor(...args) { super(...(args.length ? args : [fixed])); }
  }
  const ctx = vm.createContext({ Date: FixedDate, console, URLSearchParams, pageConfig: { useUtc }, ...extra });
  vm.runInContext(src, ctx);
  return {
    quick: (v) => vm.runInContext(`getQuickRangeDate(${JSON.stringify(v)})`, ctx),
    parse: (v) => vm.runInContext(`_parseDatetimeLocal(${JSON.stringify(v)})`, ctx),
    fromSearch: (v) => vm.runInContext(`parseRangeFromSearch(${JSON.stringify(v)})`, ctx),
    init: (v) => vm.runInContext(`initDateInputs(${JSON.stringify(v)})`, ctx),
  };
}

test("month and year presets start at the calendar boundary (UTC)", () => {
  const a = load("2026-03-31T23:30:00Z", true);
  assert.equal(a.quick("month").from.toISOString(), "2026-03-01T00:00:00.000Z");
  assert.equal(a.quick("year").from.toISOString(), "2026-01-01T00:00:00.000Z");
  // month change: the first minute of April starts a new month
  const b = load("2026-04-01T00:00:00Z", true);
  assert.equal(b.quick("month").from.toISOString(), "2026-04-01T00:00:00.000Z");
  // year change
  const c = load("2027-01-01T00:05:00Z", true);
  assert.equal(c.quick("year").from.toISOString(), "2027-01-01T00:00:00.000Z");
  // leap day
  const d = load("2028-02-29T12:00:00Z", true);
  assert.equal(d.quick("month").from.toISOString(), "2028-02-01T00:00:00.000Z");
});

test("month and year presets use local components when UTC display is off", () => {
  const a = load("2026-07-15T10:00:00Z", false);
  const m = a.quick("month").from;
  assert.deepEqual([m.getFullYear(), m.getMonth(), m.getDate(), m.getHours()], [2026, 6, 1, 0]);
  const y = a.quick("year").from;
  assert.deepEqual([y.getFullYear(), y.getMonth(), y.getDate(), y.getHours()], [2026, 0, 1, 0]);
});

test("rolling presets are unchanged", () => {
  const a = load("2026-07-15T10:00:00Z", true);
  assert.equal(a.quick("7d").to - a.quick("7d").from, 7 * 86400000);
  assert.equal(a.quick("quarter").to - a.quick("quarter").from, 90 * 86400000);
  assert.equal(a.quick("nope"), null);
});

test("datetime-local parsing rejects dates that Date() would roll over", () => {
  const a = load("2026-07-15T10:00:00Z", false);
  assert.equal(a.parse("2026-02-31T10:00"), null);
  assert.equal(a.parse("2026-13-01T10:00"), null);
  assert.equal(a.parse("2026-04-31T10:00"), null);
  assert.equal(a.parse("2026-02-29T10:00"), null);
  assert.equal(a.parse("2026-01-01T24:00"), null);
  assert.equal(a.parse("garbage"), null);
  const ok = a.parse("2028-02-29T10:30");
  assert.deepEqual([ok.getFullYear(), ok.getMonth(), ok.getDate(), ok.getHours(), ok.getMinutes()], [2028, 1, 29, 10, 30]);
});

const NOW = "2026-07-15T10:00:30Z";
const DAY = 86400000;

test("a preset in the URL resolves to the same range as the quick select", () => {
  const a = load(NOW, true);
  const r = a.fromSearch("?range=7d");
  assert.equal(r.preset, "7d");
  assert.equal(r.to - r.from, 7 * DAY);
  assert.equal(a.fromSearch("?range=24h").to - a.fromSearch("?range=24h").from, DAY);
});

test("an explicit ISO UTC range in the URL is accepted", () => {
  const a = load(NOW, false);
  const r = a.fromSearch("?from=2026-07-14T10:00:00Z&to=2026-07-15T09:30:00.250Z");
  assert.equal(r.preset, "");
  assert.equal(r.from.toISOString(), "2026-07-14T10:00:00.000Z");
  assert.equal(r.to.toISOString(), "2026-07-15T09:30:00.250Z");
});

test("invalid URL ranges are rejected (null)", () => {
  const a = load(NOW, false);
  const bad = [
    "", "?", "?range=", "?range=bogus", "?range=constructor", "?range=24H", "?range=24h%00",
    "?range=" + "a".repeat(100), "?range=%3Cscript%3Ealert(1)%3C%2Fscript%3E", "?range=__proto__",
    "?from=2026-07-14T10:00:00Z", "?to=2026-07-15T10:00:00Z",
    "?from=2026-07-15T10:00:00Z&to=2026-07-15T10:00:00Z", // from == to
    "?from=2026-07-16T10:00:00Z&to=2026-07-15T10:00:00Z", // from > to
    "?from=2024-01-01T00:00:00Z&to=2026-07-15T10:00:00Z", // beyond the 730-day maximum
    "?from=2026-02-31T00:00:00Z&to=2026-07-15T10:00:00Z", // rolled-over date
    "?from=2026-07-14T10:00:00%2B02:00&to=2026-07-15T10:00:00Z", // offset instead of Z
    "?from=2026-07-14&to=2026-07-15", "?from=1752487200000&to=1752573600000",
    "?from=javascript:alert(1)&to=2026-07-15T10:00:00Z",
    "?from=2026-07-14T10:00:00Z%22%3E%3Cimg&to=2026-07-15T10:00:00Z",
  ];
  for (const query of bad) assert.equal(a.fromSearch(query), null, query);
  assert.equal(a.fromSearch(undefined), null);
});

test("a valid preset wins over from/to; exactly the maximum span is allowed", () => {
  const a = load(NOW, false);
  assert.equal(a.fromSearch("?range=1h&from=garbage&to=garbage").preset, "1h");
  assert.notEqual(a.fromSearch("?from=2024-07-15T00:00:00Z&to=2026-07-15T00:00:00Z"), null); // 730 days
});

function initHarness(search) {
  const nodes = {
    "range-from": { value: "" }, "range-to": { value: "" },
    "quick-range": { value: "24h" }, "target-custom-range": { open: false },
  };
  const appState = { range: { from: null, to: null } };
  const calls = [];
  const a = load(NOW, false, {
    document: { getElementById: (id) => nodes[id] || null },
    appState,
    toasts: calls,
    window: { justUpToast: { error: (m) => calls.push(m) } },
  });
  a.init(search);
  return { nodes, appState, calls };
}

test("initDateInputs applies a valid URL range to inputs, select and appState", () => {
  const h = initHarness("?range=6h");
  assert.equal(h.nodes["quick-range"].value, "6h");
  assert.equal(h.nodes["target-custom-range"].open, false);
  assert.equal(h.appState.range.to - h.appState.range.from, 6 * 3600000);
  assert.match(h.nodes["range-from"].value, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);

  const x = initHarness("?from=2026-07-10T00:00:00Z&to=2026-07-11T00:00:00Z");
  assert.equal(x.nodes["quick-range"].value, "");
  assert.equal(x.nodes["target-custom-range"].open, true);
  assert.equal(x.appState.range.from.toISOString(), "2026-07-10T00:00:00.000Z");
  assert.equal(x.appState.range.to.toISOString(), "2026-07-11T00:00:00.000Z");
});

test("initDateInputs falls back to the 24 h default without a message for missing or invalid input", () => {
  for (const search of [undefined, "", "?range=evil<script>", "?from=x&to=y", "?from=2026-07-16T00:00:00Z&to=2026-07-15T00:00:00Z"]) {
    const h = initHarness(search);
    assert.equal(h.appState.range.to - h.appState.range.from, DAY, String(search));
    assert.equal(h.nodes["quick-range"].value, "24h");
    assert.deepEqual(h.calls, []);
  }
});
