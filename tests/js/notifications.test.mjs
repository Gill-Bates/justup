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
  remove() { if (this.parentElement) this.parentElement.removeChild(this); }
  contains(node) { return this === node || this.children.some(child => child.contains(node)); }
  matches(selector) {
    return selector.startsWith(".") ? this.classList.contains(selector.slice(1)) : this.tagName === selector;
  }
  querySelector(selector) {
    for (const child of this.children) {
      if (child.matches(selector)) return child;
      const nested = child.querySelector(selector);
      if (nested) return nested;
    }
    return null;
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

test("the preview names the context on a second line and never prints null", () => {
  assert.equal(N.detailLine({ body: "Created by root", actor: "root" }, "5 min ago"), "Created by root · 5 min ago");
  assert.equal(N.detailLine({ body: "", actor: "root" }, "just now"), "root · just now");
  assert.equal(N.detailLine({ body: null, actor: null }, "just now"), "just now");
  assert.equal(N.detailLine(null, ""), "");
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
  assert.match(row(0).querySelector(".ju-notif-pop-detail").textContent, /^Created by root · /);
  assert.equal(row(0).querySelector(".visually-hidden").hidden, false);
  assert.equal(row(1).querySelector(".visually-hidden").hidden, true); // read rows carry no unread text
  assert.doesNotMatch(row(1).textContent, /null|undefined|None/);
  assert.equal(row(0).querySelector(".visually-hidden").textContent, "Unread: ");
});
