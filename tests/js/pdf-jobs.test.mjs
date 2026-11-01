//
// tests/js/pdf-jobs.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/pdf-jobs.js", import.meta.url), "utf8");
const userStorageSrc = readFileSync(new URL("../../app/static/js/utils/user-storage.js", import.meta.url), "utf8");

// status: queue of per-call outcomes ({ok,...} | Error) for the job status API.
function harness({ storage = "ok", status = [], stored = {}, user = "userA" } = {}) {
  const clock = { now: 1_000_000 };
  const calls = { status: 0 };
  const events = [];
  const toasts = [];
  const created = [];
  const timers = [];
  let tick;
  const localStorage = storage === "ok"
    ? {
      getItem: key => stored[key] ?? null,
      setItem: (key, value) => { stored[key] = value; },
      removeItem: key => { delete stored[key]; },
      key: index => Object.keys(stored)[index] ?? null,
      get length() { return Object.keys(stored).length; },
    }
    : {
      getItem() { throw new Error("SecurityError"); }, setItem() { throw new Error("SecurityError"); },
      removeItem() { throw new Error("SecurityError"); }, key() { throw new Error("SecurityError"); },
      get length() { throw new Error("SecurityError"); },
    };
  const window = {
    apiFetch: async () => {
      calls.status += 1;
      const item = status.shift();
      if (item instanceof Error) throw item;
      return item;
    },
    dispatchEvent: event => events.push(event.detail),
    justUpToast: { success: m => toasts.push(["ok", m]), error: m => toasts.push(["error", m]) },
  };
  const body = { appendChild: c => { created.push(c); }, children: [] };
  const document = {
    visibilityState: "visible",
    body,
    documentElement: { getAttribute: name => (name === "data-user-key" && user ? user : null) },
    getElementById: () => null,
    addEventListener() {},
    createElement: tag => { const node = { tag }; created.push(node); return node; },
  };
  const context = vm.createContext({
    window, document, localStorage,
    Date: { now: () => clock.now },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    setTimeout: fn => { timers.push(fn); return timers.length; },
    setInterval: fn => { tick = fn; return 1; },
    console: { error() {} },
  });
  vm.runInContext(userStorageSrc, context);
  vm.runInContext(src, context);
  const run = async advance => {
    clock.now += advance;
    tick();
    await new Promise(resolve => setImmediate(resolve));
  };
  return { window, calls, events, toasts, created, run, stored, document };
}
const json = body => ({ ok: true, json: async () => body });
const completed = json({ status: "completed", filename: "r.pdf" });
const http = code => ({ ok: false, status: code });

test("a completed job ends polling and resets the button, without creating any DOM or toast", async () => {
  const h = harness({ status: [json({ status: "processing" }), completed] });
  h.window.justUpPdfJobs.add("j1", 7);
  await h.run(2000);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 1);
  await h.run(2000);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 0);
  assert.equal(h.events.length, 1);
  assert.equal(h.events[0].jobId, "j1");
  assert.equal(h.events[0].success, true);
  assert.equal(h.created.length, 0);
  assert.equal(h.toasts.length, 0);
  await h.run(2000);
  assert.equal(h.calls.status, 2); // no further polling once finished
});

test("a failed job ends polling with the server error, without DOM or toast", async () => {
  const h = harness({ status: [json({ status: "failed", error: "PDF generation failed" })] });
  h.window.justUpPdfJobs.add("j1", 7);
  await h.run(2000);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 0);
  assert.equal(h.events[0].success, false);
  assert.match(h.events[0].error, /PDF generation failed/);
  assert.equal(h.created.length, 0);
  assert.equal(h.toasts.length, 0);
});

test("the poller exposes no download or ready-list API", () => {
  const h = harness();
  assert.deepEqual(Object.keys(h.window.justUpPdfJobs).sort(), ["add", "getPending", "remove"]);
});

test("a 503 while polling keeps the job and finishes after recovery", async () => {
  const h = harness({ status: [http(503), completed] });
  h.window.justUpPdfJobs.add("j1", 7);
  await h.run(2000);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 1);
  await h.run(2000); // still backing off
  assert.equal(h.calls.status, 1);
  await h.run(10_000);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 0);
  assert.equal(h.events.at(-1).success, true);
});

test("a network error is retried instead of dropping the job", async () => {
  const h = harness({ status: [new TypeError("network"), completed] });
  h.window.justUpPdfJobs.add("j1", 7);
  await h.run(2000);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 1);
  await h.run(10_000);
  assert.equal(h.events.at(-1).success, true);
});

test("404 while polling is final: the job is removed with an error and no toast", async () => {
  const h = harness({ status: [http(404)] });
  h.window.justUpPdfJobs.add("j1", 7);
  await h.run(2000);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 0);
  assert.equal(h.events[0].success, false);
  assert.equal(h.toasts.length, 0);
});

test("repeated transient failures end with an error after the attempt limit", async () => {
  const h = harness({ status: Array.from({ length: 10 }, () => http(503)) });
  h.window.justUpPdfJobs.add("j1", 7);
  for (let i = 0; i < 6 && h.window.justUpPdfJobs.getPending().length; i++) await h.run(31_000);
  assert.equal(h.calls.status, 5);
  assert.equal(h.events.at(-1).success, false);
});

test("without localStorage access the job still completes in the session", async () => {
  const h = harness({ storage: "blocked", status: [completed] });
  h.window.justUpPdfJobs.add("j1", 7);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 1);
  await h.run(2000);
  assert.equal(h.events.at(-1).success, true);
});

test("an invalid job id is never polled or stored", async () => {
  const h = harness({ status: [completed] });
  h.window.justUpPdfJobs.add("../x?y=1", 7);
  assert.equal(h.window.justUpPdfJobs.getPending().length, 0);
  assert.deepEqual(h.stored, {});
});

const jobKeys = stored => Object.keys(stored).filter(k => k.includes("pdf_jobs"));
test("pending jobs are keyed per user; another user's entries are deleted on load", () => {
  const a = harness({ user: "userA" });
  a.window.justUpPdfJobs.add("j1", 7);
  assert.ok(jobKeys(a.stored).length === 1 && jobKeys(a.stored)[0].includes("userA"));
  const same = harness({ stored: a.stored, user: "userA" });
  assert.equal(same.window.justUpPdfJobs.getPending().length, 1);
  const b = harness({ stored: a.stored, user: "userB" });
  assert.equal(b.window.justUpPdfJobs.getPending().length, 0);
  assert.equal(Object.keys(a.stored).some(k => k.includes("userA")), false);
});
test("logout clears the user-scoped job key", () => {
  const a = harness({ user: "userA" });
  a.window.justUpPdfJobs.add("j1", 7);
  a.window.JustUpUserStorage.clearUserScoped(); // what app.js does on logout
  assert.deepEqual(a.stored, {});
});
test("legacy global keys and the old per-user ready list are deleted on load, never shown", () => {
  const entry = JSON.stringify([{ jobId: "old", targetId: 1, filename: "leak.pdf", readyAt: 1_000_000 }]);
  const stored = {
    justup_pdf_ready: entry, justup_pdf_jobs: JSON.stringify([{ jobId: "old", targetId: 1, addedAt: 1_000_000 }]),
    "justup.u.userB.pdf_ready": entry,
  };
  const h = harness({ stored, user: "userB" });
  assert.equal(h.window.justUpPdfJobs.getPending().length, 0);
  assert.deepEqual(Object.keys(stored), []);
  assert.equal(h.created.length, 0);
});
test("without a user key nothing is persisted and foreign entries are removed", async () => {
  const a = harness({ user: "userA" });
  a.window.justUpPdfJobs.add("j1", 7);
  const anon = harness({ stored: a.stored, user: "", status: [completed] });
  anon.window.justUpPdfJobs.add("j2", 7);
  await anon.run(2000);
  assert.equal(anon.events.at(-1).success, true);
  assert.deepEqual(Object.keys(a.stored), []);
});
test("a pending job survives a page navigation and is polled again", async () => {
  const first = harness({ status: [] });
  first.window.justUpPdfJobs.add("j1", 7);
  const second = harness({ stored: first.stored, status: [completed] });
  assert.equal(second.window.justUpPdfJobs.getPending().length, 1);
  await second.run(2000);
  assert.equal(second.events.at(-1).success, true);
  assert.equal(second.created.length, 0);
});
