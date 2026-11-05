//
// tests/js/service-detection.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/target-detail/service-detection.js", import.meta.url), "utf8");

function el() {
  const handlers = {};
  return {
    hidden: false, disabled: false, textContent: "", children: [], dataset: {},
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener(type, fn) { handlers[type] = fn; },
    click() { return handlers.click(); },
    append(child) { this.children.push(child); },
    replaceChildren() { this.children = []; },
  };
}

function harness(apiFetch) {
  const calls = [];
  const root = el();
  root.dataset.targetId = "3";
  const parts = { preview: el(), confirm: el(), status: el(), error: el(), changes: el() };
  const selectors = {
    '[data-service-action="preview"]': parts.preview,
    '[data-service-action="confirm"]': parts.confirm,
    "[data-service-status]": parts.status,
    "[data-service-error]": parts.error,
    "[data-service-changes]": parts.changes,
  };
  root.querySelector = (s) => selectors[s];
  const window = {
    location: { reload() {} },
    apiFetch: async (url, opts) => { calls.push([url, JSON.parse(opts.body)]); return apiFetch(url); },
  };
  const document = {
    getElementById: () => root,
    createElement: () => ({ children: [], textContent: "" }),
  };
  vm.runInNewContext(src, { window, document, Number, String, JSON, Error });
  return { calls, ...parts };
}

const reply = (body, ok = true) => ({ ok, status: ok ? 200 : 409, json: async () => body });

test("a preview without token keeps confirm hidden and reports no changes", async () => {
  const h = harness(() => reply({ token: null, changes: [] }));
  await h.preview.click();
  assert.equal(h.confirm.hidden, true);
  assert.equal(h.status.textContent, "No service changes found.");
});

test("a failed confirm hides the button and cannot be replayed with the same token", async () => {
  const h = harness((url) => (url.endsWith("/preview")
    ? reply({ token: "t1", changes: [{ kind: "identified", protocol: "tcp", port: 22, name: "ssh" }] })
    : reply({ detail: "expired" }, false)));
  await h.preview.click();
  assert.equal(h.confirm.hidden, false);
  await h.confirm.click();
  assert.equal(h.confirm.hidden, true);
  assert.equal(h.error.textContent, "expired");
  await h.confirm.click();
  assert.equal(h.calls.filter(([url]) => url.endsWith("/confirm")).length, 1);
});

test("service names render as literal text without child elements", async () => {
  const evil = "<img src=x onerror=1>";
  const h = harness(() => reply({ token: "t", changes: [{ kind: "identified", protocol: "tcp", port: 80, name: evil }] }));
  await h.preview.click();
  const item = h.changes.children[0];
  assert.equal(item.textContent, `TCP:80: identified ${evil}`);
  assert.equal(item.children.length, 0);
});
