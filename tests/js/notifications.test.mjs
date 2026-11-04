//
// tests/js/notifications.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/notifications.js", import.meta.url), "utf8");
const sandbox = { window: {} };
vm.runInNewContext(src, sandbox);
const N = sandbox.window.JustUpNotifications;

const resp = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body });

const fire = async (h) => { h.timers.at(-1).fn(); await new Promise((r) => setImmediate(r)); };

// Manual timers so the 15 s schedule and the backoff are observable without waiting.
function harness(responses, hidden = { v: false }) {
  const timers = [];
  const calls = { fetch: 0, data: [], auth: 0 };
  const poller = N.createPoller({
    intervalMs: 15000,
    fetchSummary: async () => { calls.fetch++; const r = responses.shift(); if (r instanceof Error) throw r; return r; },
    onData: (d) => calls.data.push(d),
    onAuthLost: () => { calls.auth++; },
    isHidden: () => hidden.v,
    setTimer: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimer: (id) => { timers[id - 1] = null; },
  });
  return { poller, timers, calls, hidden };
}

test("badge shows nothing at 0, the number up to 99 and 99+ above", () => {
  assert.equal(N.formatBadge(0), "");
  assert.equal(N.formatBadge(1), "1");
  assert.equal(N.formatBadge(99), "99");
  assert.equal(N.formatBadge(100), "99+");
  assert.equal(N.formatBadge(-3), "");
});

test("accessible label is dynamic", () => {
  assert.equal(N.ariaLabel(0), "Notifications");
  assert.equal(N.ariaLabel(5), "Notifications, 5 unread");
});

test("the bell shakes only for a newer unread event, never on the first response", () => {
  assert.equal(N.shouldShake(null, { unread: 3, latest_unread_id: 9 }), false);
  assert.equal(N.shouldShake({ unread: 3, latest_unread_id: 9 }, { unread: 3, latest_unread_id: 9 }), false);
  assert.equal(N.shouldShake({ unread: 3, latest_unread_id: 9 }, { unread: 4, latest_unread_id: 10 }), true);
  assert.equal(N.shouldShake({ unread: 3, latest_unread_id: 9 }, { unread: 0, latest_unread_id: 0 }), false);
});

test("the bell re-animates for a regrouped delivery that keeps its id but raises the unread sequence", () => {
  // Regression: a re-surfaced delivery group keeps the same max unread id, so latest_unread_id
  // alone never fires. The unread_event_seq rises, which must drive the shake.
  const prev = { unread: 1, latest_unread_id: 42, unread_event_seq: 3 };
  const next = { unread: 1, latest_unread_id: 42, unread_event_seq: 4 };
  assert.equal(N.shouldShake(prev, next), true);
  // No new activity: identical sequence must not shake, even with unread present.
  assert.equal(N.shouldShake(prev, { unread: 1, latest_unread_id: 42, unread_event_seq: 3 }), false);
  // A drop in the sequence (reads removed) must not shake.
  assert.equal(N.shouldShake(prev, { unread: 1, latest_unread_id: 42, unread_event_seq: 2 }), false);
  // Zero unread never shakes regardless of the sequence.
  assert.equal(N.shouldShake(prev, { unread: 0, latest_unread_id: 42, unread_event_seq: 9 }), false);
  // Fallback to id comparison when the sequence is absent from the payload.
  assert.equal(N.shouldShake({ unread: 1, latest_unread_id: 9 }, { unread: 1, latest_unread_id: 10 }), true);
});

test("polling runs immediately, then every 15 s, and backs off on errors", async () => {
  const h = harness([resp(200, { unread: 1 }), resp(500), resp(500), resp(200, { unread: 2 })]);
  await h.poller.start();
  assert.equal(h.calls.fetch, 1);
  assert.equal(h.timers.at(-1).ms, 15000);
  await fire(h);
  assert.equal(h.timers.at(-1).ms, 30000);
  await fire(h);
  assert.equal(h.timers.at(-1).ms, 60000);
  await fire(h);
  assert.equal(h.timers.at(-1).ms, 15000);
  assert.equal(h.calls.data.length, 2);
});

test("a hidden tab schedules nothing and becoming visible refreshes at once", async () => {
  const h = harness([resp(200, { unread: 1 }), resp(200, { unread: 2 })]);
  h.hidden.v = true;
  await h.poller.start();
  assert.equal(h.timers.length, 0);
  h.hidden.v = false;
  await h.poller.onVisibility();
  assert.equal(h.calls.fetch, 2);
});

test("requests never overlap", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  let started = 0;
  const poller = N.createPoller({
    intervalMs: 15000, isHidden: () => false, onData() {}, onAuthLost() {},
    setTimer: () => 1, clearTimer() {},
    fetchSummary: async () => { started++; await gate; return resp(200, {}); },
  });
  const first = poller.start();
  await poller.start();
  assert.equal(started, 1);
  release();
  await first;
});

test("401 and 403 stop polling for good", async () => {
  for (const status of [401, 403]) {
    const h = harness([resp(status)]);
    await h.poller.start();
    assert.equal(h.calls.auth, 1);
    assert.equal(h.timers.length, 0);
    await h.poller.refresh();
    assert.equal(h.calls.fetch, 1);
  }
});

test("a stale response is dropped after a refresh", async () => {
  let releaseOld;
  const old = new Promise((r) => { releaseOld = r; });
  const seen = [];
  const queue = [async () => { await old; return resp(200, { id: "old" }); }, async () => resp(200, { id: "new" })];
  const poller = N.createPoller({
    intervalMs: 15000, isHidden: () => false, onAuthLost() {}, setTimer: () => 1, clearTimer() {},
    onData: (d) => seen.push(d.id), fetchSummary: () => queue.shift()(),
  });
  const first = poller.start();
  await poller.refresh();
  releaseOld();
  await first;
  assert.deepEqual(seen, ["new"]);
});

test("reduced motion is honoured in the stylesheet", () => {
  const css = readFileSync(new URL("../../app/static/css/notifications.css", import.meta.url), "utf8");
  assert.match(css, /prefers-reduced-motion: reduce\)[\s\S]*animation: none/);
});

test("refreshes during a running request never overlap and cause exactly one follow-up", async () => {
  const releases = [];
  let running = 0, maxRunning = 0, started = 0;
  const poller = N.createPoller({
    intervalMs: 15000, isHidden: () => false, onData() {}, onAuthLost() {},
    setTimer: () => 1, clearTimer() {},
    fetchSummary: () => new Promise((resolve) => {
      started++; running++; maxRunning = Math.max(maxRunning, running);
      releases.push(() => { running--; resolve(resp(200, {})); });
    }),
  });
  const first = poller.start();
  await poller.refresh();
  await poller.refresh();
  assert.equal(started, 1);
  releases[0]();
  await new Promise((r) => setImmediate(r));
  assert.equal(started, 2); // the queued refresh runs after, not during, the first request
  releases[1]();
  await first;
  assert.equal(maxRunning, 1);
  assert.equal(started, 2);
});

class MiniElement {
  constructor(tagName, ownerDocument) {
    this.tagName = tagName;
    this.ownerDocument = ownerDocument;
    this.children = [];
    this.parentElement = null;
    this.dataset = {};
    this.className = "";
    this._text = "";
    this.moves = 0;
    this.classList = { contains: name => this.className.split(/\s+/).includes(name) };
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  set textContent(value) { this._text = String(value); this.children = []; }
  appendChild(child) { return this.insertBefore(child, null); }
  insertBefore(child, before) {
    if (child.parentElement) {
      if (child.parentElement === this && this.children[this.children.indexOf(child) + 1] === before) return child;
      if (this.ownerDocument.activeElement && child.contains(this.ownerDocument.activeElement)) {
        this.ownerDocument.activeElement = null; // moving an attached DOM node can drop descendant focus
      }
      child.parentElement.removeChild(child);
    }
    const index = before ? this.children.indexOf(before) : -1;
    if (index === -1) this.children.push(child);
    else this.children.splice(index, 0, child);
    child.parentElement = this;
    this.moves++;
    return child;
  }
  removeChild(child) {
    this.children = this.children.filter(candidate => candidate !== child);
    if (child.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = null;
    child.parentElement = null; return child;
  }
  setAttribute(name, value) { this.attrs = { ...this.attrs, [name]: String(value) }; }
  getAttribute(name) { return this.attrs && name in this.attrs ? this.attrs[name] : null; }
  removeAttribute(name) { if (this.attrs) delete this.attrs[name]; }
  remove() { if (this.parentElement) this.parentElement.removeChild(this); }
  closest(selector) {
    for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node;
    return null;
  }
  contains(node) { return this === node || this.children.some(child => child.contains(node)); }
  matches(selector) {
    return selector.startsWith(".") ? this.classList.contains(selector.slice(1)) : this.tagName.toLowerCase() === selector;
  }
  querySelector(selector) {
    for (const child of this.children) {
      if (child.matches(selector)) return child;
      const nested = child.querySelector(selector);
      if (nested) return nested;
    }
    return null;
  }
  querySelectorAll(selector) {
    const found = [];
    for (const child of this.children) {
      if (child.matches(selector)) found.push(child);
      found.push(...child.querySelectorAll(selector));
    }
    return found;
  }
  focus() {
    if (!this.ownerDocument.roots.some(root => root.contains(this))) { this.focusedWhileDetached = true; return; }
    this.ownerDocument.activeElement = this;
  }
}

const previewDocument = () => {
  const doc = { activeElement: null, roots: [] };
  doc.createElement = tag => new MiniElement(tag, doc);
  return doc;
};

test("preview polling reuses focused controls and moves focus if the item leaves the preview", () => {
  const doc = previewDocument();
  const list = doc.createElement("ul");
  doc.roots.push(list);
  const bell = doc.createElement("button");
  const item = (id, title) => ({ id, title, severity: "info", is_read: false, created_at: "2026-01-01T00:00:00Z" });
  N.renderPreview(list, { items: [item(1, "First"), item(2, "Second")] }, doc, bell);
  const second = list.children[1].querySelector(".ju-notif-pop-btn");
  second.focus();

  N.renderPreview(list, { items: [item(2, "Second updated"), item(1, "First")] }, doc, bell);
  assert.equal(doc.activeElement, second);
  assert.equal(list.children[0].querySelector(".ju-notif-pop-btn"), second);
  assert.match(second.textContent, /Second updated/);
  const moveCount = list.moves;
  N.renderPreview(list, { items: [item(2, "Second updated"), item(1, "First")] }, doc, bell);
  assert.equal(list.moves, moveCount); // stable order must not detach focused rows again

  N.renderPreview(list, { items: [item(1, "First")] }, doc, bell);
  assert.equal(doc.activeElement, list.children[0].querySelector(".ju-notif-pop-btn"));
});

test("focus moves to a newly created preview row only after it is attached", () => {
  const doc = previewDocument();
  const list = doc.createElement("ul");
  doc.roots.push(list);
  const bell = doc.createElement("button");
  const item = id => ({ id, title: "Notification " + id, severity: "info", is_read: false, created_at: "2026-01-01T00:00:00Z" });
  N.renderPreview(list, { items: [item("old")] }, doc, bell);
  list.children[0].querySelector(".ju-notif-pop-btn").focus();

  N.renderPreview(list, { items: [item("new")] }, doc, bell);
  const replacement = list.children[0].querySelector(".ju-notif-pop-btn");
  assert.equal(replacement.focusedWhileDetached, undefined);
  assert.equal(doc.activeElement, replacement);
  assert.equal(list.contains(replacement), true);
});

test("the context line carries only the body/actor and never prints null", () => {
  // The time is rendered as a separate <time> element now, so detailLine returns context only.
  assert.equal(N.detailLine({ body: "Created by root", actor: "root" }), "Created by root");
  assert.equal(N.detailLine({ body: "", actor: "root" }), "root");
  assert.equal(N.detailLine({ body: null, actor: null }), "");
  assert.equal(N.detailLine(null), "");
  assert.equal(N.detailLine({ title: "Same text", body: "same TEXT" }), "", "context repeating the title is dropped");
});

test("every category has an icon and unknown ones fall back to the bell", () => {
  for (const c of ["monitoring", "reports", "delivery", "security", "activity"]) assert.notEqual(N.categoryIcon(c), "notifications");
  assert.equal(N.categoryIcon("other"), "notifications");
});

test("preview rows show context, icon and a screen-reader-only unread marker; markup stays literal text", () => {
  const doc = previewDocument();
  const list = doc.createElement("ul");
  doc.roots.push(list);
  const bell = doc.createElement("button");
  const evil = "<img src=x onerror=alert(1)>";
  const base = { severity: "info", created_at: "2026-01-01T00:00:00Z" };
  N.renderPreview(list, { items: [
    { ...base, id: 1, category: "activity", title: "Target created: " + evil, body: "Created by root", actor: "root", is_read: false },
    { ...base, id: 2, category: "security", title: "Old row", body: null, actor: null, is_read: true },
  ] }, doc, bell);
  const row = (i) => list.children[i].querySelector(".ju-notif-pop-btn");
  assert.equal(row(0).querySelector(".material-icons").textContent, "history");
  assert.equal(row(1).querySelector(".material-icons").textContent, "shield");
  assert.equal(row(0).querySelector(".ju-notif-pop-text").textContent, "Target created: " + evil); // literal text, not markup
  assert.equal(row(0).querySelector(".ju-notif-pop-text").title, "Target created: " + evil);
  assert.equal(row(0).querySelector(".ju-notif-pop-context").textContent, "Created by root");
  assert.equal(row(0).querySelector(".visually-hidden").hidden, false);
  assert.equal(row(1).querySelector(".visually-hidden").hidden, true); // read rows carry no unread text
  assert.doesNotMatch(row(1).textContent, /null|undefined|None/);
  assert.equal(row(0).querySelector(".visually-hidden").textContent, "Unread: ");
});

test("every preview row carries a user-specific delete control wired to its notification id", () => {
  const doc = previewDocument();
  const list = doc.createElement("ul");
  doc.roots.push(list);
  const bell = doc.createElement("button");
  const base = { severity: "info", category: "activity", created_at: "2026-01-01T00:00:00Z", is_read: false };
  N.renderPreview(list, { items: [{ ...base, id: 7, title: "Seven" }, { ...base, id: 8, title: "Eight" }] }, doc, bell);
  const rows = list.children;
  assert.match(rows[0].className, /\bju-notif-swipe-item\b/);
  assert.match(rows[0].querySelector(".ju-notif-pop-btn").className, /\bju-notif-swipe-content\b/);
  const del = (i) => rows[i].querySelector(".ju-notif-dismiss-button");
  assert.equal(del(0).dataset.notificationId, "7");
  assert.equal(del(1).dataset.notificationId, "8");
  assert.equal(del(0).attrs["aria-label"], "Delete notification: Seven");
  assert.ok(rows[0].querySelector(".ju-notif-dismiss-action").contains(del(0)));
  // A refresh keeps the same control (and its focus) and only updates the label.
  N.renderPreview(list, { items: [{ ...base, id: 7, title: "Seven!" }, { ...base, id: 8, title: "Eight" }] }, doc, bell);
  assert.equal(list.children[0].querySelector(".ju-notif-dismiss-button"), del(0));
  assert.equal(del(0).attrs["aria-label"], "Delete notification: Seven!");
});

test("focus on a delete control that leaves the preview moves to a neighbouring row", () => {
  const doc = previewDocument();
  const list = doc.createElement("ul");
  doc.roots.push(list);
  const bell = doc.createElement("button");
  const item = (id) => ({ id, title: "N" + id, severity: "info", category: "activity", is_read: false, created_at: "2026-01-01T00:00:00Z" });
  N.renderPreview(list, { items: [item(1), item(2)] }, doc, bell);
  list.children[0].querySelector(".ju-notif-dismiss-button").focus();
  N.renderPreview(list, { items: [item(2)] }, doc, bell);
  assert.equal(doc.activeElement, list.children[0].querySelector(".ju-notif-pop-btn"));
});

function swipeHarness() {
  const handlers = {};
  const clickHandlers = [];
  const classes = new Set();
  const dismissButton = {};
  const item = {
    classList: { add: (n) => classes.add(n), remove: (n) => classes.delete(n), contains: (n) => classes.has(n) },
    contains: (n) => n === item || n === dismissButton,
  };
  const container = {
    dataset: {},
    addEventListener: (name, fn) => { handlers[name] = fn; },
    contains: () => true,
  };
  const timers = [];
  const doc = { addEventListener: (name, fn) => { if (name === "click") clickHandlers.push(fn); } };
  const sandbox = { window: { document: doc, setTimeout: (fn) => { timers.push(fn); return timers.length; } } };
  vm.runInNewContext(src, sandbox);
  sandbox.window.JustUpNotifications.bindSwipeRows(container);
  const target = { closest: (sel) => (sel === ".ju-notif-swipe-item" ? item : null) };
  return { handlers, clickHandlers, classes, container, target, item, dismissButton, timers };
}

test("a left swipe reveals the delete action, a right swipe or outside click hides it", () => {
  const h = swipeHarness();
  const swipe = (from, to) => {
    h.handlers.pointerdown({ target: h.target, clientX: from, clientY: 10, pointerId: 1, pointerType: "touch" });
    h.handlers.pointerup({ clientX: to, clientY: 12, pointerId: 1 });
  };
  swipe(200, 100);
  assert.equal(h.classes.has("is-dismiss-revealed"), true);
  swipe(100, 200);
  assert.equal(h.classes.has("is-dismiss-revealed"), false);
  swipe(200, 100);
  h.clickHandlers[0]({ target: { closest: () => null }, preventDefault() {}, stopPropagation() {} });
  assert.equal(h.classes.has("is-dismiss-revealed"), false);
});

test("short or mostly vertical gestures neither reveal nor swallow the click", () => {
  const h = swipeHarness();
  h.handlers.pointerdown({ target: h.target, clientX: 200, clientY: 10, pointerId: 1, pointerType: "touch" });
  h.handlers.pointerup({ clientX: 170, clientY: 10, pointerId: 1 });
  h.handlers.pointerdown({ target: h.target, clientX: 200, clientY: 10, pointerId: 2, pointerType: "touch" });
  h.handlers.pointerup({ clientX: 120, clientY: 150, pointerId: 2 });
  assert.equal(h.classes.has("is-dismiss-revealed"), false);
  let prevented = false;
  h.clickHandlers[0]({ target: { closest: (sel) => (sel === ".ju-notif-swipe-content" ? {} : null) }, preventDefault() { prevented = true; }, stopPropagation() {} });
  assert.equal(prevented, false);
});

test("a touch tap does not reveal the delete action", () => {
  const h = swipeHarness();
  h.handlers.pointerdown({ target: h.target, clientX: 200, clientY: 10, pointerId: 1, pointerType: "touch" });
  h.handlers.pointerup({ clientX: 200, clientY: 10, pointerId: 1 });
  assert.equal(h.classes.has("is-dismiss-revealed"), false);
});

test("the click that ends a swipe is swallowed once", () => {
  const h = swipeHarness();
  h.handlers.pointerdown({ target: h.target, clientX: 200, clientY: 10, pointerId: 1, pointerType: "touch" });
  h.handlers.pointerup({ clientX: 100, clientY: 10, pointerId: 1 });
  let prevented = 0;
  const click = { target: { closest: (sel) => (sel === ".ju-notif-swipe-content" ? {} : null) }, preventDefault() { prevented++; }, stopPropagation() {} };
  h.item.contains = () => true;
  h.clickHandlers[0](click);
  h.clickHandlers[0](click);
  assert.equal(prevented, 1);
});

test("keyboard focus on the delete control reveals its row", () => {
  const h = swipeHarness();
  const dismiss = { closest: (sel) => (sel === ".ju-notif-dismiss-button" ? dismiss : sel === ".ju-notif-swipe-item" ? h.item : null) };
  h.handlers.focusin({ target: dismiss });
  assert.equal(h.classes.has("is-dismiss-revealed"), true);
});

test("a right swipe on a closed row calls onSwipeRead when provided", () => {
  const h = swipeHarness();
  const seen = [];
  const container = { dataset: {}, addEventListener: (n, fn) => { h.handlers[n] = fn; }, contains: () => true };
  const sandbox = { window: { document: { addEventListener() {} }, setTimeout() {} } };
  vm.runInNewContext(src, sandbox);
  sandbox.window.JustUpNotifications.bindSwipeRows(container, { onSwipeRead: (item) => seen.push(item) });
  h.handlers.pointerdown({ target: h.target, clientX: 100, clientY: 10, pointerId: 1, pointerType: "touch" });
  h.handlers.pointerup({ clientX: 200, clientY: 12, pointerId: 1 });
  assert.deepEqual(seen, [h.item]);
});

test("mouse dragging does not reveal the delete action", () => {
  const h = swipeHarness();
  h.handlers.pointerdown({ target: h.target, clientX: 200, clientY: 10, pointerId: 1, pointerType: "mouse" });
  h.handlers.pointerup({ clientX: 100, clientY: 10, pointerId: 1 });
  assert.equal(h.classes.has("is-dismiss-revealed"), false);
});

test("a ready-PDF preview row carries its attachment URL; other rows carry none", () => {
  const doc = previewDocument();
  const list = doc.createElement("ul");
  doc.roots.push(list);
  const bell = doc.createElement("button");
  const base = { severity: "success", is_read: false, created_at: "2026-01-01T00:00:00Z" };
  const url = "/ui/reports/jobs/j1/download";
  N.renderPreview(list, { items: [{ ...base, id: 1, title: "PDF ready", action: { type: "download", url } }] }, doc, bell);
  const button = () => list.children[0].querySelector(".ju-notif-pop-btn");
  assert.equal(button().dataset.url, url);
  N.renderPreview(list, { items: [{ ...base, id: 1, title: "PDF ready", action: { type: "unavailable", label: "Report expired" } }] }, doc, bell);
  assert.equal(button().dataset.url, undefined);
});

// --- Relative time: formatting, boundaries and singular/plural ---------------------------------

const MIN = 60 * 1000, HOUR = 60 * MIN, DAY = 24 * HOUR;
const iso = (ms) => new Date(ms).toISOString();

test("relTime renders compact English ages", () => {
  const now = Date.parse("2026-06-15T12:00:00Z");
  const at = (ago) => N.relTime(iso(now - ago), now);
  assert.equal(at(0), "Now");
  assert.equal(at(59 * 1000), "Now"); // still under a minute
  assert.equal(at(1 * MIN), "1m ago");
  assert.equal(at(15 * MIN), "15m ago");
  assert.equal(at(59 * MIN), "59m ago");
  assert.equal(at(1 * HOUR), "1h ago");
  assert.equal(at(3 * HOUR), "3h ago");
  assert.equal(at(23 * HOUR), "23h ago");
  assert.equal(at(1 * DAY), "1d ago");
  assert.equal(at(7 * DAY), "7d ago");
  assert.equal(at(120 * DAY), "120d ago");
});

test("relTime crosses each unit boundary cleanly", () => {
  const now = Date.parse("2026-06-15T12:00:00Z");
  const at = (ago) => N.relTime(iso(now - ago), now);
  assert.equal(at(60 * 1000 - 1), "Now"); // just under one minute
  assert.equal(at(60 * 1000), "1m ago"); // exactly one minute
  assert.equal(at(HOUR - 1), "59m ago"); // just under one hour
  assert.equal(at(HOUR), "1h ago");
  assert.equal(at(DAY - 1), "23h ago"); // just under one day
  assert.equal(at(DAY), "1d ago");
});

test("relTime never shows a missing, invalid or future timestamp as Now", () => {
  const now = Date.parse("2026-06-15T12:00:00Z");
  assert.equal(N.relTime(undefined, now), "");
  assert.equal(N.relTime(null, now), "");
  assert.equal(N.relTime("", now), "");
  assert.equal(N.relTime("not-a-date", now), "");
  // A slightly future timestamp (clock skew) must stay blank, not read as "Now".
  assert.equal(N.relTime(iso(now + 5 * MIN), now), "");
});

test("absTime formats the full timestamp and honours UTC mode, blank when unparseable", () => {
  assert.equal(N.absTime("not-a-date", false), "");
  const utc = N.absTime("2026-06-15T09:05:00Z", true);
  assert.match(utc, /2026/);
  assert.match(utc, /\bUTC$/);
  assert.doesNotMatch(N.absTime("2026-06-15T09:05:00Z", false), /UTC$/);
});

test("a preview row renders the time as its own <time> element, context stays separate", () => {
  const doc = previewDocument();
  const list = doc.createElement("ul");
  doc.roots.push(list);
  const bell = doc.createElement("button");
  const created = "2026-06-15T09:00:00Z";
  N.renderPreview(list, { items: [
    { id: 1, title: "Report ready", body: "A long description that would otherwise push the time off", actor: "root", severity: "info", category: "reports", is_read: false, created_at: created },
  ] }, doc, bell);
  const row = list.children[0].querySelector(".ju-notif-pop-btn");
  const timeEl = row.querySelector(".ju-notif-pop-time");
  assert.ok(timeEl, "the row carries a dedicated time element");
  assert.equal(timeEl.tagName, "time");
  assert.equal(timeEl.getAttribute("datetime"), created); // unmodified original ISO
  assert.ok(/ago$|^Now$/.test(timeEl.textContent), "shows a relative age");
  assert.match(timeEl.title, /Created:/); // full timestamp available as a tooltip
  // Context lives in its own span and does not carry the time.
  const ctx = row.querySelector(".ju-notif-pop-context");
  assert.equal(ctx.textContent, "A long description that would otherwise push the time off");
  assert.doesNotMatch(ctx.textContent, /ago|Now/);
});

test("an invalid created_at leaves the time element empty and hidden, never Now", () => {
  const doc = previewDocument();
  const list = doc.createElement("ul");
  doc.roots.push(list);
  const bell = doc.createElement("button");
  N.renderPreview(list, { items: [
    { id: 1, title: "Broken", body: "ctx", severity: "info", category: "activity", is_read: false, created_at: "not-a-date" },
  ] }, doc, bell);
  const timeEl = list.children[0].querySelector(".ju-notif-pop-time");
  assert.equal(timeEl.textContent, "");
  assert.equal(timeEl.hidden, true);
  assert.equal(timeEl.getAttribute("datetime"), null);
  assert.doesNotMatch(timeEl.textContent, /Now/);
});

test("refreshRelativeTimes updates ages from the datetime attribute without rebuilding the list", () => {
  const doc = previewDocument();
  const list = doc.createElement("ul");
  doc.roots.push(list);
  const bell = doc.createElement("button");
  const created = "2026-06-15T09:00:00Z";
  N.renderPreview(list, { items: [{ id: 1, title: "T", body: "ctx", severity: "info", category: "activity", is_read: false, created_at: created }] }, doc, bell);
  const timeEl = list.children[0].querySelector(".ju-notif-pop-time");
  const control = list.children[0].querySelector(".ju-notif-pop-btn");
  const movesBefore = list.moves;

  // One hour after creation the visible age must change to "1h ago" without any re-render.
  N.refreshRelativeTimes(list, doc, Date.parse(created) + HOUR);
  assert.equal(timeEl.textContent, "1h ago");
  assert.equal(list.moves, movesBefore); // no DOM node was moved or recreated
  assert.equal(list.children[0].querySelector(".ju-notif-pop-btn"), control); // same control, focus-safe

  // Three hours: plural.
  N.refreshRelativeTimes(list, doc, Date.parse(created) + 3 * HOUR);
  assert.equal(timeEl.textContent, "3h ago");
});

test("refreshRelativeTimes ignores a time element without a datetime and never writes an empty age", () => {
  const doc = previewDocument();
  const list = doc.createElement("ul");
  const bare = doc.createElement("time");
  bare.className = "ju-notif-pop-time";
  bare.textContent = "7d ago";
  list.appendChild(bare);
  doc.roots.push(list);
  // No datetime attribute: it must be left untouched (not blanked).
  N.refreshRelativeTimes(list, doc, Date.now());
  assert.equal(bare.textContent, "7d ago");
});

test("the public API exposes the relative formatter so the time logic is not duplicated", () => {
  // DRY: one formatter, shared and testable, rather than a second copy of the rules.
  assert.equal(typeof N.relTime, "function");
  assert.equal(typeof N.absTime, "function");
  assert.equal(typeof N.refreshRelativeTimes, "function");
});
