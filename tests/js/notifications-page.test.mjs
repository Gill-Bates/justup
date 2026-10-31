//
// tests/js/notifications-page.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/notifications-page.js", import.meta.url), "utf8");

function harness(apiFetch) {
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
  };
  vm.runInNewContext(src, { window, document, Event: class Event { constructor(type) { this.type = type; } } });
  return { handlers, events, timers, navigations, urls, appended };
}

const tick = () => new Promise(resolve => setImmediate(resolve));

test("marking an unread notification removes its New badge immediately", async () => {
  const h = harness(async () => ({ ok: true }));
  const badge = { removed: false, remove() { this.removed = true; } };
  const item = { removedClass: null, classList: { remove(name) { item.removedClass = name; } },
    querySelector: selector => selector === ".ju-notif-title .text-bg-primary" ? badge : null };
  const mark = { dataset: { id: "n-1" }, removed: false, closest: selector => selector === ".ju-notif-item" ? item : null,
    remove() { this.removed = true; } };
  const event = { target: { closest: selector => selector === ".np-mark" ? mark : null } };
  h.handlers.click(event);
  await tick();
  assert.equal(item.removedClass, "is-unread");
  assert.equal(badge.removed, true);
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

test("authorized PDF download revokes its object URL after triggering the download", async () => {
  const blob = { size: 8 };
  const h = harness(async url => url === "/report.pdf"
    ? { ok: true, blob: async () => blob }
    : { ok: true });
  const download = { dataset: { id: "n-3", url: "/report.pdf" } };
  h.handlers.click({ target: { closest: selector => selector === ".np-download" ? download : null } });
  await tick();
  await tick();
  assert.equal(h.urls.created.length, 1);
  assert.equal(h.appended[0].clicked, true);
  assert.deepEqual(h.urls.revoked, []);
  h.timers[0]();
  assert.deepEqual(h.urls.revoked, [h.urls.created[0].url]);
  assert.equal(h.events.length, 1);
});
