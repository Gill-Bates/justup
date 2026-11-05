//
// tests/js/dashboard-downtime.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/dashboard.js", import.meta.url), "utf8");
const dashboardTemplate = readFileSync(new URL("../../app/templates/dashboard.html", import.meta.url), "utf8");
const IDS = ["table-container", "success", "empty", "error", "loading", "count-badge", "table-body"];

function el(id) {
  const classes = new Set(id === "table-container" || id === "success" || id === "empty" || id === "error" ? ["d-none"] : []);
  const listeners = {};
  return {
    id, textContent: "", innerHTML: "", children: [],
    listeners,
    classList: {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
      contains: name => classes.has(name),
    },
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(name, callback) { (listeners[name] ||= []).push(callback); },
    dispatch(name, event) { (listeners[name] || []).forEach(callback => callback(event)); },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    attributes: {},
    dataset: {},
  };
}

function harness(responses) {
  const nodes = Object.fromEntries(IDS.map(id => [id, el(id)]));
  let ready;
  let liveRefresh;
  const bodyListeners = {};
  const document = {
    body: {
      addEventListener(name, callback) { (bodyListeners[name] ||= []).push(callback); },
      removeEventListener(name, callback) {
        bodyListeners[name] = (bodyListeners[name] || []).filter(listener => listener !== callback);
      },
      dispatchEvent(event) {
        (bodyListeners[event.type] || []).forEach(callback => callback(event));
      },
    },
    getElementById: id => nodes[id.replace(/^downtime-/, "")] || null,
    createElement: () => el("x"),
    createTextNode: text => ({ textContent: text }),
    addEventListener: (name, fn) => { if (name === "DOMContentLoaded") ready = fn; },
    visibilityState: "visible",
  };
  const requests = [];
  const window = {
    JustUpLive: {
      client: {
        subscribe(_events, callback) { liveRefresh = callback; return () => {}; },
        state: () => "connected",
      },
    },
    apiFetch: async url => {
      requests.push(url);
      const next = responses.shift();
      if (next instanceof Error) throw next;
      return next;
    },
    setInterval: () => 1,
  };
  vm.runInNewContext(src, {
    window, document, console: { error() {}, warn() {} }, setInterval: window.setInterval, AbortSignal,
  });
  const load = async () => { ready(); await new Promise(resolve => setImmediate(resolve)); };
  const visible = id => !nodes[id].classList.contains("d-none");
  const dispatchBodyEvent = event => document.body.dispatchEvent(event);
  return { nodes, load, visible, requests, dispatchBodyEvent, refreshLive: () => liveRefresh() };
}

const ok = body => ({ ok: true, json: async () => body });
const incident = { target_id: 1, name: "web", services: { http_up: { downtime_seconds: 300, incidents: 1, ongoing: false } } };

test("a successful response shows the table with the affected count", async () => {
  const h = harness([ok({ targets: [incident] })]);
  await h.load();
  assert.equal(h.visible("table-container"), true);
  assert.equal(h.visible("error"), false);
  assert.equal(h.nodes["count-badge"].textContent, "1 affected");
  assert.equal(h.requests[0].includes("limit="), false);
});

test("a failure after a success shows the error state instead of stale rows or the empty state", async () => {
  for (const failure of [{ ok: false, status: 500 }, { ok: true, json: async () => { throw new SyntaxError("bad"); } }]) {
    const h = harness([ok({ targets: [incident] }), failure]);
    await h.load();
    assert.equal(h.visible("table-container"), true);
    await h.load();
    assert.equal(h.visible("error"), true);
    assert.equal(h.visible("table-container"), false);
    assert.equal(h.visible("empty"), false);
    assert.equal(h.visible("count-badge"), false);
  }
});

test("a live refresh during a pending load triggers one follow-up request", async () => {
  let resolveInitial;
  const latest = { ...incident, name: "updated" };
  const h = harness([
    new Promise(resolve => { resolveInitial = resolve; }),
    ok({ targets: [incident, latest] }),
  ]);
  await h.load();
  h.refreshLive();
  resolveInitial(ok({ targets: [incident] }));
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(h.requests.length, 2);
  assert.equal(h.nodes["count-badge"].textContent, "2 affected");
});

test("dashboard has no manual refresh button", () => {
  assert.doesNotMatch(dashboardTemplate, /id="dashboard-refresh-btn"/);
});

test("HTMX swaps do not restart a fade on dashboard content", () => {
  const h = harness([]);
  const swapped = el("dashboard-status-online");
  h.dispatchBodyEvent({ type: "htmx:afterSwap", detail: { elt: swapped } });
  assert.equal(swapped.classList.contains("fade-in"), false);
});
