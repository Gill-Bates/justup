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
function load(nowIso, useUtc) {
  const RealDate = Date;
  const fixed = new RealDate(nowIso).getTime();
  class FixedDate extends RealDate {
    constructor(...args) { super(...(args.length ? args : [fixed])); }
  }
  const ctx = vm.createContext({ Date: FixedDate, console, pageConfig: { useUtc } });
  vm.runInContext(src, ctx);
  return {
    quick: (v) => vm.runInContext(`getQuickRangeDate(${JSON.stringify(v)})`, ctx),
    parse: (v) => vm.runInContext(`_parseDatetimeLocal(${JSON.stringify(v)})`, ctx),
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
