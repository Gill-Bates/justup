//
// tests/js/spooler.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/settings.spooler.js", import.meta.url), "utf8");

function makeEl(extra = {}) {
  const listeners = {};
  return {
    value: "", textContent: "", innerHTML: "", disabled: false, isConnected: true,
    classList: { contains: () => true },
    addEventListener(type, fn) { (listeners[type] ??= []).push(fn); },
    count: (type) => (listeners[type] ?? []).length,
    fire: (type) => (listeners[type] ?? []).forEach((fn) => fn({ preventDefault() {} })),
    querySelectorAll: () => [],
    ...extra,
  };
}

function build() {
  let dom = {};
  const intervals = new Map();
  let nextTimer = 1;
  const requests = [];
  const ctx = vm.createContext({
    console: { error() {} },
    escapeHtml: (s) => String(s),
    URLSearchParams,
    AbortController,
    setInterval: (fn) => { intervals.set(nextTimer, fn); return nextTimer++; },
    clearInterval: (id) => intervals.delete(id),
    apiFetch: (url, opts) => new Promise((resolve, reject) => {
      const req = { url, signal: opts?.signal, resolve, reject };
      opts?.signal?.addEventListener?.("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
      requests.push(req);
    }),
    confirm: () => true,
  });
  ctx.window = ctx;
  ctx.document = { getElementById: (id) => dom[id] ?? null };
  const fragment = () => {
    dom = {};
    for (const id of ["spooler-stat-queue", "spooler-refresh-btn", "spooler-flush-btn",
      "confirm-spooler-clear-log-btn", "spooler-filter-status", "spooler-filter-channel",
      "spooler-log-body", "spooler-pagination", "spooler-log-info", "spooler-pane"]) dom[id] = makeEl();
    return dom;
  };
  return { ctx, fragment, intervals, requests, current: () => dom };
}

test("repeated init on the same DOM keeps one listener set and one poller", () => {
  const h = build();
  const dom = h.fragment();
  vm.runInContext(src, h.ctx);
  h.ctx._initSpoolerTab();
  h.ctx._initSpoolerTab();
  h.ctx._initSpoolerTab();
  assert.equal(dom["spooler-refresh-btn"].count("click"), 1);
  assert.equal(dom["spooler-filter-status"].count("change"), 1);
  assert.equal(h.intervals.size, 1);
});

test("a replaced fragment is rebound and the old poller is cleared", () => {
  const h = build();
  h.fragment();
  vm.runInContext(src, h.ctx);
  h.ctx._initSpoolerTab();
  const fresh = h.fragment();
  h.ctx._initSpoolerTab();
  assert.equal(fresh["spooler-refresh-btn"].count("click"), 1);
  assert.equal(h.intervals.size, 1);
});

test("the poller stops itself once the bound fragment is gone", () => {
  const h = build();
  const dom = h.fragment();
  vm.runInContext(src, h.ctx);
  h.ctx._initSpoolerTab();
  dom["spooler-stat-queue"].isConnected = false;
  [...h.intervals.values()][0]();
  assert.equal(h.intervals.size, 0);
});

test("an older log response never overwrites a newer one", async () => {
  const h = build();
  const dom = h.fragment();
  vm.runInContext(src, h.ctx);
  h.ctx._initSpoolerTab();
  const logs = () => h.requests.filter((r) => r.url.includes("/log"));
  const before = logs().length;
  dom["spooler-filter-status"].value = "failed";
  dom["spooler-filter-status"].fire("change");
  assert.equal(logs().length, before + 1);
  const [older, newer] = logs().slice(-2);
  const body = (name) => ({ ok: true, json: async () => ({ total: 1, entries: [{ notification_type: "alert", status: "sent", channel_type: "email", recipient_address: name, retry_count: 0 }] }) });
  newer.resolve(body("newer@example"));
  await new Promise((r) => setImmediate(r));
  older.resolve(body("older@example"));
  await new Promise((r) => setImmediate(r));
  assert.match(dom["spooler-log-body"].innerHTML, /newer@example/);
  assert.doesNotMatch(dom["spooler-log-body"].innerHTML, /older@example/);
});
