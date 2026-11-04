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
  ctx.document = { getElementById: (id) => dom[id] ?? null, querySelectorAll: () => [] };
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

test("log timestamps preserve existing UTC and non-UTC offsets", async () => {
  const h = build();
  const dom = h.fragment();
  vm.runInContext(src, h.ctx);
  h.ctx._initSpoolerTab();
  const request = h.requests.filter((item) => item.url.includes("/log")).at(-1);
  const timestamps = ["2026-11-01T19:30:00+00:00", "2026-11-01T19:30:00+02:00"];
  request.resolve({ ok: true, json: async () => ({ total: timestamps.length, entries: timestamps.map((updated_at) => ({
    notification_type: "alert", status: "sent", channel_type: "email", recipient_address: "ops@example.com", retry_count: 0, updated_at,
  })) }) });
  await new Promise((resolve) => setImmediate(resolve));
  for (const timestamp of timestamps) {
    const expected = new Date(timestamp).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    assert.ok(dom["spooler-log-body"].innerHTML.includes(expected));
  }
});
test("failed handoffs with an empty active queue are not shown as healthy", async () => {
  const h = build();
  const dom = h.fragment();
  for (const id of ["spooler-health", "spooler-health-icon", "spooler-health-text"]) dom[id] = makeEl();
  vm.runInContext(src, h.ctx);
  h.ctx._initSpoolerTab();
  const stats = h.requests.filter((r) => r.url.includes("/stats")).at(-1);
  stats.resolve({ ok: true, json: async () => ({ pending: 0, processing: 0, failed: 3, failed_recent: 3, sent: 5, channels: {}, cooldowns: [] }) });
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(dom["spooler-health-text"].textContent, /3 handoff failure/);
  assert.equal(dom["spooler-health-icon"].textContent, "error_outline");
  assert.match(dom["spooler-health"].className, /text-danger/);
});
test("old failures outside the 24 h window do not turn the state red", () => {
  const h = build();
  h.fragment();
  vm.runInContext(src, h.ctx);
  const { healthState } = h.ctx._spoolerInternals;
  assert.match(healthState({ pending: 0, processing: 0, failed: 3, failed_recent: 0, cooldowns: [] }).tone, /body-secondary/);
  assert.match(healthState({ failed: 3, cooldowns: [] }).tone, /danger/);
  assert.match(healthState({ failed_recent: 0, max_retry_count: 2, cooldowns: [] }).text, /retried/);
});
test("status labels say accepted/handoff and never claim delivery", () => {
  const h = build();
  h.fragment();
  vm.runInContext(src, h.ctx);
  const { statusBadge } = h.ctx._spoolerInternals;
  assert.match(statusBadge("sent", 0), />\s*Sent<\/span>$/);
  assert.doesNotMatch(statusBadge("sent", 0), /title=/);
  assert.match(statusBadge("pending", 0), /Waiting for handoff/);
  assert.match(statusBadge("pending", 2), /Retrying handoff/);
  assert.match(statusBadge("failed", 3), /Handoff failed/);
  for (const status of ["sent", "pending", "failed", "processing"]) assert.doesNotMatch(statusBadge(status, 1), /[Dd]elivered/);
});

test("channel column is icon-only with an accessible name", () => {
  const h = build();
  h.fragment();
  vm.runInContext(src, h.ctx);
  const { channelIcon } = h.ctx._spoolerInternals;
  for (const [type, icon, name] of [["email", "email", "Email"], ["signal", "chat", "Signal"]]) {
    const html = channelIcon(type);
    assert.match(html, new RegExp(`title="${name}"`));
    assert.match(html, new RegExp(`aria-hidden="true">${icon}</span>`));
    assert.match(html, new RegExp(`<span class="visually-hidden">${name}</span>`));
    assert.equal(html.replace(/<[^>]*>/g, "").replace(icon, "").replace(name, ""), "", "no further visible text");
  }
});

test("status label 'Sent' is used in the template and 'Accepted by server' is gone", () => {
  const tpl = readFileSync(new URL("../../app/templates/settings/spooler.html", import.meta.url), "utf8");
  assert.match(tpl, /<option value="sent">Sent<\/option>/);
  assert.match(tpl, /text-uppercase">Sent<\/div>/);
  assert.doesNotMatch(tpl + src, /Accepted by server/);
});
