//
// tests/js/notifications-page.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/notifications-page.js", import.meta.url), "utf8");

function harness(apiFetch, extra = {}) {
  const handlers = {};
  const events = [];
  const timers = [];
  const navigations = [];
  const urls = { created: [], revoked: [] };
  const page = { addEventListener: (name, fn) => { handlers[name] = fn; } };
  const appended = [];
  const body = { appendChild: el => { appended.push(el); el.parent = body; } };
  const document = {
    body,
    getElementById: id => id === "notifications-page" ? page : null,
    createElement: tag => ({ tagName: tag, click() { this.clicked = true; }, remove() { this.removed = true; } }),
  };
  const window = {
    apiFetch,
    dispatchEvent: event => events.push(event),
    location: { assign: href => navigations.push(href) },
    URL: {
      createObjectURL: blob => { const url = "blob:test-" + urls.created.length; urls.created.push({ url, blob }); return url; },
      revokeObjectURL: url => urls.revoked.push(url),
    },
    setTimeout: fn => { timers.push(fn); return timers.length; },
    ...extra,
  };
  window.location.reload = () => { window.reloaded = (window.reloaded || 0) + 1; };
  window.JustUpNotifications = { bindSwipeRows: el => { window.swipeBound = el; } };
  vm.runInNewContext(src, { window, document, Event: class Event { constructor(type) { this.type = type; } } });
  return { handlers, events, timers, navigations, urls, appended, window, page };
}

const tick = () => new Promise(resolve => setImmediate(resolve));

test("marking an unread notification removes its unread marker immediately", async () => {
  const h = harness(async () => ({ ok: true }));
  const dot = { removed: false, remove() { this.removed = true; } };
  const hidden = { removed: false, remove() { this.removed = true; } };
  const item = { removedClass: null, classList: { remove(name) { item.removedClass = name; } },
    querySelector: selector => ({ ".ju-unread-dot": dot, ".ju-notif-title .visually-hidden": hidden })[selector] || null };
  const mark = { dataset: { id: "n-1" }, removed: false, closest: selector => selector === ".ju-notif-item" ? item : null,
    remove() { this.removed = true; } };
  const event = { target: { closest: selector => selector === ".np-mark" ? mark : null } };
  h.handlers.click(event);
  await tick();
  assert.equal(item.removedClass, "is-unread");
  assert.equal(dot.removed, true);
  assert.equal(hidden.removed, true);
  assert.equal(mark.removed, true);
});

test("linked notification waits for the read request before mouse or keyboard navigation", async () => {
  for (const button of [0, -1]) {
    let finishRead;
    const h = harness(() => new Promise(resolve => { finishRead = resolve; }));
    const open = { dataset: { id: "n-2" }, href: "/reports/jobs/job-2" };
    let prevented = false;
    h.handlers.click({
      target: { closest: selector => selector === ".np-open" ? open : null },
      button,
      preventDefault() { prevented = true; },
    });
    await tick();
    assert.equal(prevented, true);
    assert.deepEqual(h.navigations, []);
    finishRead({ ok: true });
    await tick();
    assert.deepEqual(h.navigations, ["/reports/jobs/job-2"]);
    assert.equal(h.events.length, 1);
  }
});

test("modified linked-notification clicks keep the browser's native new-tab action", async () => {
  let finishRead;
  const h = harness(() => new Promise(resolve => { finishRead = resolve; }));
  const open = { dataset: { id: "n-4" }, href: "/reports/jobs/job-4" };
  let prevented = false;
  h.handlers.click({
    target: { closest: selector => selector === ".np-open" ? open : null },
    button: 0,
    metaKey: true,
    preventDefault() { prevented = true; },
  });
  await tick();
  assert.equal(prevented, false);
  finishRead({ ok: true });
  await tick();
  assert.deepEqual(h.navigations, []);
});

test("authorized PDF download hands over to the PDF helper and marks read only afterwards", async () => {
  const calls = [];
  const h = harness(async url => { calls.push(url); return { ok: true }; }, {
    justUpPdfJobs: { startDownload: async (url, name) => { calls.push(["download", url, name]); } },
  });
  const download = { dataset: { id: "n-3", url: "/ui/reports/jobs/j/download" } };
  h.handlers.click({ target: { closest: selector => selector === ".np-download" ? download : null } });
  await tick();
  await tick();
  assert.deepEqual(calls, [["download", "/ui/reports/jobs/j/download", "report.pdf"], "/ui/notifications/n-3/read"]);
  assert.equal(h.events.length, 1);
});

test("a failed PDF download does not mark the notification read and shows an error", async () => {
  const toasts = [];
  const reads = [];
  const h = harness(async url => { reads.push(url); return { ok: true }; }, {
    justUpPdfJobs: { startDownload: async () => { throw new Error("gone"); } },
    justUpToast: { error: m => toasts.push(m) },
  });
  h.handlers.click({ target: { closest: selector => selector === ".np-download" ? { dataset: { id: "n-9", url: "/x" } } : null } });
  await tick();
  await tick();
  assert.deepEqual(reads, []);
  assert.deepEqual(toasts, ["Report is no longer available"]);
});

const dismissEvent = button => ({
  target: { closest: selector => selector === ".ju-notif-dismiss-button" ? button : null },
  preventDefault() { this.prevented = true; },
});

test("the page binds swipe handling to the list container", () => {
  const h = harness(async () => ({ ok: true }));
  assert.equal(h.window.swipeBound, h.page);
});

test("delete posts to the dismiss endpoint through apiFetch (CSRF header), notifies the bell and reloads", async () => {
  const requests = [];
  const h = harness(async (url, options) => { requests.push([url, options]); return { ok: true }; });
  const button = { dataset: { notificationId: "42" }, disabled: false };
  const event = dismissEvent(button);
  h.handlers.click(event);
  assert.equal(event.prevented, true);
  assert.equal(button.disabled, true);
  await tick();
  assert.equal(JSON.stringify(requests), JSON.stringify([["/ui/notifications/42/dismiss", { method: "POST" }]]));
  assert.equal(h.events.length, 1);
  assert.equal(h.events[0].type, "justup:notifications-changed");
  assert.equal(h.window.reloaded, 1);
});

test("a failed delete re-enables the button, shows an error and keeps the page", async () => {
  const toasts = [];
  const h = harness(async () => ({ ok: false, status: 404 }), { justUpToast: { error: m => toasts.push(m) } });
  const button = { dataset: { notificationId: "5" }, disabled: false };
  h.handlers.click(dismissEvent(button));
  await tick();
  assert.equal(button.disabled, false);
  assert.deepEqual(toasts, ["Notification could not be deleted"]);
  assert.equal(h.window.reloaded, undefined);
  assert.equal(h.events.length, 0);
});

test("the delete id is URL-encoded", async () => {
  const urls = [];
  const h = harness(async url => { urls.push(url); return { ok: true }; });
  h.handlers.click(dismissEvent({ dataset: { notificationId: "1/../2" }, disabled: false }));
  await tick();
  assert.deepEqual(urls, ["/ui/notifications/1%2F..%2F2/dismiss"]);
});
