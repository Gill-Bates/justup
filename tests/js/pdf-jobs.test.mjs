//
// tests/js/pdf-jobs.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/pdf-jobs.js", import.meta.url), "utf8");

// routes: array of per-call outcomes ({status, body} | Error) keyed by "status" or "download".
function harness({ storage = "ok", status = [], download = [] } = {}) {
  const clock = { now: 1_000_000 };
  const calls = { status: 0, download: 0 };
  const events = [];
  const toasts = [];
  const clicks = [];
  let tick;
  const stored = {};
  const localStorage = storage === "ok"
    ? { getItem: key => stored[key] ?? null, setItem: (key, value) => { stored[key] = value; } }
    : { getItem() { throw new Error("SecurityError"); }, setItem() { throw new Error("SecurityError"); } };
  const next = (queue, kind) => {
    calls[kind] += 1;
    const item = queue.shift();
    if (item instanceof Error) throw item;
    return item;
  };
  const window = {
    apiFetch: async url => {
      if (url.endsWith("/download")) return next(download, "download");
      return next(status, "status");
    },
    dispatchEvent: event => events.push(event.detail),
    justUpToast: { success: m => toasts.push(["ok", m]), error: m => toasts.push(["error", m]) },
  };
  const document = {
    visibilityState: "visible",
    body: { appendChild() {}, removeChild() {} },
    createElement: () => ({ click() { clicks.push(this.download); } }),
  };
  vm.runInNewContext(src, {
    window, document, localStorage,
    Date: { now: () => clock.now },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
    setTimeout: () => 0,
    setInterval: fn => { tick = fn; return 1; },
    console: { error() {} },
  });
  const run = async advance => {
    clock.now += advance;
    tick();
    await new Promise(resolve => setImmediate(resolve));
  };
  return { window, calls, events, toasts, clicks, run };
}

const json = body => ({ ok: true, json: async () => body });
const completed = json({ status: "completed", filename: "r.pdf" });
const pdf = { ok: true, blob: async () => ({}) };
const http = code => ({ ok: false, status: code });

test("a 503 while polling keeps the job and succeeds after recovery", async () => {
  const h = harness({ status: [http(503), completed], download: [pdf] });
  h.window.justUpPdfJobs.add("j1", 7);
  await h.run(2000);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 1);
  await h.run(2000); // still backing off
  assert.equal(h.calls.status, 1);
  await h.run(10_000);
  assert.deepEqual(h.clicks, ["r.pdf"]);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 0);
  assert.equal(h.events.at(-1).success, true);
});

test("a 503 on download keeps the finished job until the retry downloads it", async () => {
  const h = harness({ status: [completed, completed], download: [http(503), pdf] });
  h.window.justUpPdfJobs.add("j1", 7);
  await h.run(2000);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 1);
  assert.equal(h.clicks.length, 0);
  await h.run(10_000);
  assert.deepEqual(h.clicks, ["r.pdf"]);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 0);
});

test("a network error is retried instead of dropping the job", async () => {
  const h = harness({ status: [new TypeError("network"), completed], download: [pdf] });
  h.window.justUpPdfJobs.add("j1", 7);
  await h.run(2000);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 1);
  await h.run(10_000);
  assert.equal(h.clicks.length, 1);
});

test("404 is final: the job is removed with an error", async () => {
  const h = harness({ status: [http(404)] });
  h.window.justUpPdfJobs.add("j1", 7);
  await h.run(2000);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 0);
  assert.equal(h.toasts[0][0], "error");
  assert.equal(h.events[0].success, false);
});

test("repeated transient failures end with an error after the attempt limit", async () => {
  const h = harness({ status: Array.from({ length: 10 }, () => http(503)) });
  h.window.justUpPdfJobs.add("j1", 7);
  for (let i = 0; i < 6 && h.window.justUpPdfJobs.getPending().length; i++) await h.run(31_000);
  assert.equal(h.calls.status, 5);
  assert.equal(h.events.at(-1).success, false);
});

test("without localStorage access the job is still polled and downloaded in the session", async () => {
  const h = harness({ storage: "blocked", status: [completed], download: [pdf] });
  h.window.justUpPdfJobs.add("j1", 7);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 1);
  await h.run(2000);
  assert.deepEqual(h.clicks, ["r.pdf"]);
  assert.equal(h.events.at(-1).success, true);
});
