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
  window.JustUpNotifications = { bindSwipeRows: (el, opts) => { window.swipeBound = el; window.swipeOpts = opts; } };
  vm.runInNewContext(src, { window, document, Event: class Event { constructor(type) { this.type = type; } } });
  return { handlers, events, timers, navigations, urls, appended, window, page };
}

const tick = () => new Promise(resolve => setImmediate(resolve));

const rowEvent = (content, extra = {}) => ({
  target: { closest: selector => selector === ".ju-notif-swipe-content.is-actionable" ? content : null },
  preventDefault() { this.prevented = true; },
  ...extra,
});

function unreadRow(action, data = {}) {
  const removed = [];
  const classes = new Set(["is-unread"]);
  const dot = { remove() { removed.push("dot"); } };
  const hidden = { remove() { removed.push("hidden"); } };
  const item = { dataset: { id: "n-1" }, classList: { remove: n => classes.delete(n), contains: n => classes.has(n) },
    querySelector: s => ({ ".ju-unread-dot": dot, ".ju-notif-title .visually-hidden": hidden, ".ju-notif-swipe-content": content })[s] || null };
  const attrs = [];
  const { href, ...dataset } = data;
  const content = { tagName: action === "link" || action === "download" ? "A" : "DIV", dataset: { id: "n-1", action, ...dataset }, getAttribute: name => name === "href" ? href : null, classList: { remove() {} }, removeAttribute: a => attrs.push(a),
    closest: s => s === ".ju-notif-item" ? item : null };
  return { item, content, removed, classes, attrs };
}

test("a plain unread row marks itself read on click and drops its unread markers", async () => {
  const h = harness(async () => ({ ok: true }));
  const row = unreadRow("read");
  h.handlers.click(rowEvent(row.content));
  await tick();
  assert.equal(row.classes.has("is-unread"), false);
  assert.deepEqual(row.removed, ["dot", "hidden"]);
  assert.equal(h.events.length, 1);
});

test("the right-swipe hook and ArrowRight mark the row read", async () => {
  const h = harness(async () => ({ ok: true }));
  const row = unreadRow("link", { href: "/ui/targets/1" });
  h.window.swipeOpts.onSwipeRead(row.item);
  await tick();
  assert.equal(row.classes.has("is-unread"), false);
  const row2 = unreadRow("link", { href: "/ui/targets/1" });
  let prevented = false;
  row2.content.closest = sel => sel === ".ju-notif-swipe-content.is-actionable" ? row2.content : row2.item;
  h.handlers.keydown({ key: "ArrowRight", target: row2.content, preventDefault() { prevented = true; } });
  await tick();
  assert.equal(prevented, true);
  assert.equal(row2.classes.has("is-unread"), false);
});

test("unmodified click on a linked notification marks read, then navigates", async () => {
  let finishRead;
  const h = harness(() => new Promise(resolve => { finishRead = resolve; }));
  const row = unreadRow("link", { href: "/ui/targets/7" });
  const event = rowEvent(row.content);
  h.handlers.click(event);
  await tick();
  assert.equal(event.prevented, true);
  assert.deepEqual(h.navigations, []);
  finishRead({ ok: true });
  await tick();
  assert.deepEqual(h.navigations, ["/ui/targets/7"]);
  assert.equal(h.events.length, 1);
});
test("a linked notification leaves Enter and Space to the native anchor", async () => {
  const h = harness(async () => ({ ok: true }));
  const row = unreadRow("link", { href: "/ui/targets/7" });
  row.content.closest = s => s === ".ju-notif-swipe-content.is-actionable" ? row.content : row.item;
  for (const key of ["Enter", " "]) {
    let prevented = false;
    h.handlers.keydown({ key, target: row.content, preventDefault() { prevented = true; } });
    assert.equal(prevented, false);
  }
  await tick();
  assert.deepEqual(h.navigations, []);
});
test("modified and middle clicks keep native link behavior and only record the read state", async () => {
  const reads = [];
  const h = harness(async url => { reads.push(url); return { ok: true }; });
  const row = unreadRow("link", { href: "/ui/targets/7" });
  const modified = rowEvent(row.content, { metaKey: true });
  h.handlers.click(modified);
  const middle = { button: 1, target: modified.target, preventDefault() { this.prevented = true; } };
  h.handlers.auxclick(middle);
  const right = { button: 2, target: modified.target, preventDefault() {} };
  h.handlers.auxclick(right);
  await tick();
  assert.equal(modified.prevented, undefined);
  assert.equal(middle.prevented, undefined);
  assert.deepEqual(h.navigations, []);
  assert.deepEqual(reads, ["/ui/notifications/n-1/read", "/ui/notifications/n-1/read"]);
});
test("a download row is left to the browser and only records the read state", async () => {
  const calls = [];
  const h = harness(async url => { calls.push(url); return { ok: true }; });
  const row = unreadRow("download", { href: "/ui/reports/jobs/j/download" });
  row.content.dataset.id = "n-3";
  const event = rowEvent(row.content);
  h.handlers.click(event);
  await tick();
  assert.equal(event.prevented, undefined);
  assert.deepEqual(h.navigations, []);
  assert.deepEqual(h.appended, []);
  assert.deepEqual(h.urls.created, []);
  assert.deepEqual(calls, ["/ui/notifications/n-3/read"]);
  assert.equal(row.classes.has("is-unread"), false);
  assert.equal(h.events.length, 1);
});
test("a failed read request on a download row keeps the row unread and shows no dialog", async () => {
  const h = harness(async () => ({ ok: false }));
  const row = unreadRow("download", { href: "/x" });
  h.handlers.click(rowEvent(row.content));
  await tick();
  assert.equal(row.classes.has("is-unread"), true);
  assert.equal(h.events.length, 0);
});
test("Enter on a download anchor is not intercepted", () => {
  const h = harness(async () => ({ ok: true }));
  const row = unreadRow("download", { href: "/x" });
  row.content.closest = sel => sel === ".ju-notif-swipe-content.is-actionable" ? row.content : row.item;
  const event = { key: "Enter", target: row.content, preventDefault() { this.prevented = true; } };
  h.handlers.keydown(event);
  assert.equal(event.prevented, undefined);
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
