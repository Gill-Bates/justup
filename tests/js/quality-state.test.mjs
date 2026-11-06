//
// tests/js/quality-state.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/utils/quality-state.js", import.meta.url), "utf8");
const ctx = vm.createContext({});
ctx.window = ctx;
vm.runInContext(src, ctx);
const describe = (data) => ctx.QualityState.describe(data);

test("unknown state renders status dot and score neutral grey", () => {
  const v = describe({ state: "unknown", score: 42.5 });
  assert.equal(v.badgeTone, "neutral");
  assert.equal(v.badgeText, "Unknown");
  assert.equal(v.scoreClass, "dashboard-widget-value fw-bold");
});

test("missing or incomplete data is grey, never red", () => {
  for (const data of [null, undefined, {}, { state: "" }, { state: "bogus", score: null },
    { state: "down", score: null }, { state: "ok", score: "" }, { state: "ok", score: "abc" }]) {
    const v = describe(data);
    assert.equal(v.state, "unknown");
    assert.equal(v.badgeTone, "neutral");
    assert.equal(v.scoreClass, "dashboard-widget-value fw-bold");
    assert.equal(v.scoreText, "--");
  }
});

test("an unrecognised state with a score is grey but keeps the measured number", () => {
  const v = describe({ state: "bogus", score: 50 });
  assert.equal(v.badgeTone, "neutral");
  assert.equal(v.scoreClass, "dashboard-widget-value fw-bold");
  assert.equal(v.scoreText, "50.0");
});

test("status dots keep their semantic tones while scores remain neutral", () => {
  assert.equal(describe({ state: "ok", score: 90 }).badgeTone, "success");
  assert.equal(describe({ state: "ok", score: 90 }).scoreClass, "dashboard-widget-value fw-bold");
  assert.equal(describe({ state: "degraded", score: 55 }).badgeTone, "warning");
  assert.equal(describe({ state: "degraded", score: 55 }).scoreClass, "dashboard-widget-value fw-bold");
  const down = describe({ state: "down", score: 10 });
  assert.equal(down.scoreClass, "dashboard-widget-value fw-bold");
  assert.equal(down.badgeTone, "danger");
  assert.equal(describe({ state: "ok", score: 90 }).scoreText, "90.0");
});

test("unknown state always has a neutral description, whatever the score", () => {
  for (const score of [95, 40, null, 0]) {
    assert.equal(describe({ state: "unknown", score }).label, "Connectivity status unavailable");
  }
  assert.equal(describe(null).label, "Connectivity status unavailable");
});

test("measured states keep their score-based description", () => {
  assert.equal(describe({ state: "ok", score: 90 }).label, "Excellent connectivity");
  assert.equal(describe({ state: "degraded", score: 65 }).label, "Good connectivity");
  assert.equal(describe({ state: "down", score: 10 }).label, "Poor connectivity");
});
test("target badge without country is labelled unknown/anycast, with country keeps the code", () => {
  const unknown = ctx.QualityState.describeTarget("1.1.1.1", null);
  assert.match(unknown.label, /^Country unknown \(anycast or no location data\): 1\.1\.1\.1$/);
  assert.ok(unknown.title);
  assert.deepEqual({ ...ctx.QualityState.describeTarget("8.8.8.8", "US") }, { label: "US: 8.8.8.8", title: "" });
});
