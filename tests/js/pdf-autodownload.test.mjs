//
// tests/js/pdf-autodownload.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const read = rel => readFileSync(new URL(`../../${rel}`, import.meta.url), "utf8");
const src = read("app/static/js/pdf-jobs.js");
const userStorageSrc = read("app/static/js/utils/user-storage.js");

const memoryStore = (data = {}) => ({
  getItem: k => data[k] ?? null,
  setItem: (k, v) => { data[k] = v; },
  removeItem: k => { delete data[k]; },
  key: i => Object.keys(data)[i] ?? null,
  get length() { return Object.keys(data).length; },
});

// One "tab": own sessionStorage, shared localStorage (pass `local` to model a second tab / reload).
function tab({ local = {}, session = {}, hideDuringPoll = false, headStatus = 200, quotaFull = false } = {}) {
  const clock = { now: 1_000_000 };
  const heads = [];
  const frames = [];
  const timers = [];
  const listeners = {};
  let tick;
  const document = {
    visibilityState: "visible",
    documentElement: { getAttribute: n => (n === "data-user-key" ? "userA" : null) },
    body: { appendChild: f => frames.push(f) },
    addEventListener: (type, fn) => { listeners[type] = fn; },
    createElement: tag => ({ tag, remove() { this.removed = true; }, setAttribute() {} }),
    getElementById: () => null,
  };
  const localStore = memoryStore(local);
  if (quotaFull) localStore.setItem = () => { throw new Error("QuotaExceededError"); };
  const spies = { createObjectURL: 0, blob: 0, open: 0, navigated: 0 };
  const window = {
    apiFetch: async () => {
      if (hideDuringPoll) document.visibilityState = "hidden";
      return { ok: true, json: async () => ({ status: "completed" }) };
    },
    fetch: async (url, init) => { heads.push([url, init.method]); return { ok: headStatus < 400, status: headStatus }; },
    dispatchEvent: () => {},
    sessionStorage: memoryStore(session),
    open: () => { spies.open += 1; },
    location: { assign: () => { spies.navigated += 1; } },
  };
  const context = vm.createContext({
    window, document, localStorage: localStore, sessionStorage: window.sessionStorage,
    Date: { now: () => clock.now },
    CustomEvent: class { constructor(t, i) { this.detail = i.detail; } },
    setTimeout: (fn, ms) => { timers.push([fn, ms]); return timers.length; },
    setInterval: fn => { tick = fn; return 1; },
    console: { error() {}, warn() {} },
    URL: { createObjectURL() { spies.createObjectURL += 1; } },
    Blob: class { constructor() { spies.blob += 1; } },
  });
  vm.runInContext(userStorageSrc, context);
  vm.runInContext(src, context);
  const poll = async () => {
    clock.now += 2000;
    tick();
    await new Promise(r => setImmediate(r));
    await new Promise(r => setImmediate(r));
  };
  const show = () => { document.visibilityState = "visible"; listeners.visibilitychange(); };
  return { window, document, heads, frames, timers, spies, poll, show, local, session };
}

const URL_OF = id => `/ui/reports/jobs/${id}/download`;

test("the originating tab starts the download once through a hidden iframe", async () => {
  const t = tab();
  t.window.justUpPdfJobs.add("j1", 7);
  await t.poll();
  await t.poll();
  assert.deepEqual(t.heads, [[URL_OF("j1"), "HEAD"]]);
  assert.equal(t.frames.length, 1);
  assert.equal(t.frames[0].tag, "iframe");
  assert.equal(t.frames[0].hidden, true);
  assert.equal(t.frames[0].src, URL_OF("j1"));
  assert.equal(t.spies.open + t.spies.navigated, 0);
});

test("the iframe is removed after its lifetime", async () => {
  const t = tab();
  t.window.justUpPdfJobs.add("j1", 7);
  await t.poll();
  const [cleanup, ms] = t.timers.at(-1);
  assert.ok(ms >= 10000);
  cleanup();
  assert.equal(t.frames[0].removed, true);
});

test("a hidden page defers the download until it becomes visible", async () => {
  const t = tab({ hideDuringPoll: true });
  t.window.justUpPdfJobs.add("j1", 7);
  await t.poll();
  assert.equal(t.heads.length, 0);
  t.show();
  await new Promise(r => setImmediate(r));
  assert.equal(t.frames.length, 1);
  t.show();
  await new Promise(r => setImmediate(r));
  assert.equal(t.frames.length, 1);
});

test("a reload of the originating tab does not download a second time", async () => {
  const first = tab();
  first.window.justUpPdfJobs.add("j1", 7);
  await first.poll();
  assert.equal(first.frames.length, 1);
  // Same tab (sessionStorage) and same user storage; the job is polled again as completed.
  const reloaded = tab({ local: first.local, session: first.session });
  reloaded.window.justUpPdfJobs.add("j1", 7);
  await reloaded.poll();
  assert.equal(reloaded.frames.length, 0);
});

test("a second tab never auto-starts a job it did not start", async () => {
  const origin = tab();
  origin.window.justUpPdfJobs.add("j1", 7);
  const other = tab({ local: origin.local });
  await other.poll();
  assert.equal(other.frames.length, 0);
  assert.equal(other.heads.length, 0);
});

test("a failed availability check leaves the page untouched", async () => {
  const t = tab({ headStatus: 410 });
  t.window.justUpPdfJobs.add("j1", 7);
  await t.poll();
  assert.equal(t.heads.length, 1);
  assert.equal(t.frames.length, 0);
  assert.equal(t.spies.navigated + t.spies.open, 0);
});

test("a transient HEAD failure does not use up the claim", async () => {
  const t = tab();
  const ok = t.window.fetch;
  t.window.fetch = async () => { throw new TypeError("network"); };
  t.window.justUpPdfJobs.add("j1", 7);
  await t.poll();
  await new Promise(setImmediate);
  assert.equal(t.frames.length, 0);
  assert.deepEqual(Object.keys(t.local).filter(k => k.includes("pdf_auto_done")), []);
  t.window.fetch = ok;
  t.window.justUpPdfJobs.add("j1", 7);
  await t.poll();
  assert.deepEqual(t.heads, [[URL_OF("j1"), "HEAD"]]);
  assert.equal(t.frames.length, 1);
  assert.equal(t.frames[0].src, URL_OF("j1"));
});

test("a claim that cannot be stored starts no download", async () => {
  const t = tab({ quotaFull: true });
  t.window.justUpPdfJobs.add("j1", 7);
  await t.poll();
  assert.equal(t.heads.length, 1);
  assert.equal(t.frames.length, 0);
});

test("a deferred download survives a reload of the hidden tab", async () => {
  const first = tab({ hideDuringPoll: true });
  first.window.justUpPdfJobs.add("j1", 7);
  await first.poll();
  assert.equal(first.frames.length, 0);
  const reloaded = tab({ local: first.local, session: first.session });
  await new Promise(r => setImmediate(r));
  assert.equal(reloaded.frames.length, 1);
  assert.equal(reloaded.frames[0].src, URL_OF("j1"));
});

test("no Blob, object URL or window.open is touched", async () => {
  const t = tab();
  t.window.justUpPdfJobs.add("j1", 7);
  await t.poll();
  assert.deepEqual([t.spies.createObjectURL, t.spies.blob, t.spies.open], [0, 0, 0]);
});

test("logout clears the markers of both storages", async () => {
  const t = tab();
  t.window.justUpPdfJobs.add("j1", 7);
  await t.poll();
  assert.ok(Object.keys(t.session).some(k => k.includes("pdf_auto")));
  assert.ok(Object.keys(t.local).some(k => k.includes("pdf_auto_done")));
  t.window.JustUpUserStorage.clearUserScoped();
  assert.deepEqual(Object.keys(t.session), []);
  assert.deepEqual(Object.keys(t.local), []);
});

test("PDF download modules contain no blob-based download path", () => {
  const banned = /createObjectURL|\bBlob\b|\.blob\s*\(|FileReader|saveAs|window\.open|\.save\s*\(|data:application\/pdf|blob:/;
  for (const rel of ["app/static/js/pdf-jobs.js", "app/static/js/target-detail/pdf-export.js"]) {
    const code = read(rel).replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
    assert.equal(banned.test(code), false, `${rel} must not build a blob/data download`);
  }
});
