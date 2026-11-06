//
// tests/js/settings-purge.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Password-confirmed purge dialogs in Settings > Storage (TSDB metrics and alerts).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

function el(extra = {}) {
  const listeners = {};
  return {
    value: "", dataset: {}, style: {}, textContent: "", disabled: false, innerHTML: "",
    classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
    addEventListener(type, fn) { (listeners[type] ??= []).push(fn); },
    fire: async (type, event = { preventDefault() {} }) => { for (const fn of listeners[type] ?? []) await fn(event); },
    setAttribute() {}, removeAttribute() {}, getAttribute: () => null, replaceChildren() {}, appendChild() {}, append() {},
    querySelector: () => null, querySelectorAll: () => [], closest: () => null,
    ...extra,
  };
}
const nodes = {};
for (const id of ["purge-form", "purge-password", "purge-error", "confirm-purge-btn", "purgeModal",
  "purge-alerts-form", "purge-alerts-password", "purge-alerts-error", "confirm-purge-alerts-btn", "purgeAlertsModal", "settings-form",
  "backup-btn", "restore-btn", "restore-file"]) {
  nodes[id] = el();
}
globalThis.document = {
  readyState: "complete", body: { addEventListener() {} }, addEventListener() {},
  getElementById: (id) => nodes[id] ?? null, querySelector: () => null, querySelectorAll: () => [], createElement: () => el(),
};
globalThis.CSS = { escape: (s) => s };
const toasts = [];
const requests = [];
let reply;
globalThis.window = {
  justUpToast: { success: (m) => toasts.push(["success", m]), warning() {}, error() {} },
  apiFetch: async (url, options) => { requests.push([url, JSON.parse(options.body)]); return reply(); },
  bootstrap: undefined,
};
globalThis.setTimeout = () => 0;
globalThis.location = { reload() {} };

vm.runInContext(readFileSync(new URL("../../app/static/js/utils/api-error.js", import.meta.url), "utf8"), vm.createContext(globalThis.window));

const { initStorage } = await import("../../app/static/js/settings.storage.js");
initStorage();

const respond = (ok, body, status = ok ? 200 : 400) => () => ({ ok, status, json: async () => body, text: async () => JSON.stringify(body) });
const dialogs = [
  { name: "metrics", form: "purge-form", password: "purge-password", error: "purge-error", url: "/settings/tsdb/purge", success: "TSDB data deleted successfully.", okBody: {} },
  { name: "alerts", form: "purge-alerts-form", password: "purge-alerts-password", error: "purge-alerts-error", url: "/settings/alerts/purge", success: "3 alerts deleted successfully.", okBody: { deleted: 3 } },
];

for (const d of dialogs) {
  test(`${d.name} purge: an empty password is rejected without a request`, async () => {
    requests.length = 0; toasts.length = 0;
    nodes[d.password].value = "";
    await nodes[d.form].fire("submit");
    assert.equal(requests.length, 0);
    assert.equal(nodes[d.error].textContent, "Please enter your password.");
  });

  test(`${d.name} purge: success posts the password and reports it`, async () => {
    requests.length = 0; toasts.length = 0;
    nodes[d.password].value = "secret";
    reply = respond(true, d.okBody);
    await nodes[d.form].fire("submit");
    assert.deepEqual(requests, [[d.url, { password: "secret" }]]);
    assert.deepEqual(toasts, [["success", d.success]]);
  });

  test(`${d.name} purge: a server error is shown in the dialog and nothing is reported`, async () => {
    requests.length = 0; toasts.length = 0;
    nodes[d.password].value = "wrong";
    reply = respond(false, { detail: "Invalid password" }, 403);
    await nodes[d.form].fire("submit");
    assert.equal(nodes[d.error].textContent, "Invalid password");
    assert.deepEqual(toasts, []);
  });
}

test("backup tab buttons are bound by script, once, without inline handlers", async () => {
  let picked = 0;
  nodes["restore-file"].click = () => { picked++; };
  initStorage(); // HTMX settle re-runs init: must not bind twice
  await nodes["restore-btn"].fire("click");
  assert.equal(picked, 1);
  assert.equal(nodes["backup-btn"].dataset.clickBound, "1");
  assert.equal(nodes["restore-file"].dataset.changeBound, "1");
});
