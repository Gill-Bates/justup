//
// tests/js/settings-autosave-retry.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import test from "node:test";

function el(extra = {}) {
  const listeners = {};
  return {
    value: "", dataset: {}, style: {}, textContent: "", disabled: false, innerHTML: "",
    classList: { add() {}, remove() {}, contains: () => false },
    addEventListener(type, fn) { (listeners[type] ??= []).push(fn); },
    fire: async (type, event) => { for (const fn of listeners[type] ?? []) await fn(event); },
    setAttribute() {}, replaceChildren() {}, appendChild() {}, append() {},
    querySelectorAll: () => [],
    ...extra,
  };
}
const form = el();
const nodes = { "settings-form": form };
globalThis.document = {
  readyState: "complete",
  body: { addEventListener() {} },
  addEventListener() {},
  getElementById: (id) => nodes[id] ?? null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => el(),
};
globalThis.CSS = { escape: (s) => s };
let responses = [];
const sent = [];
globalThis.window = {
  justUpToast: { success() {}, warning() {}, error() {} },
  apiFetch: async (url, opts) => {
    sent.push(JSON.parse(opts.body));
    return responses.shift();
  },
};
globalThis.setTimeout = () => 0;
globalThis.location = { reload() {} };

const { initStorage } = await import("../../app/static/js/settings.storage.js");
initStorage();

const ok = { ok: true, status: 200, json: async () => ({}) };
const fail = { ok: false, status: 500, json: async () => ({}) };
const input = (type, name, value, defaultValue) => ({
  type, name, value, defaultValue, min: "1", max: "60", tagName: "INPUT",
  autocomplete: "", closest: () => null, hasAttribute: () => false,
  checkValidity() { return this.value !== "" && Number(this.value) >= 1 && Number(this.value) <= 60; },
  reportValidity() { this.reported = true; },
});

test("a failed text save is retried on the next blur of the unchanged value", async () => {
  sent.length = 0;
  responses = [fail, ok, ok];
  const field = input("text", "smtp_host", "mail.example", "");
  await form.fire("blur", { target: field });
  await form.fire("blur", { target: field });
  assert.equal(sent.length, 2);
  await form.fire("blur", { target: field });
  assert.equal(sent.length, 2, "confirmed value is not saved again");
});

test("a failed number save is retried on the next blur of the unchanged value", async () => {
  sent.length = 0;
  responses = [fail, ok];
  const field = input("number", "retention_days", "30", "7");
  await form.fire("blur", { target: field });
  await form.fire("blur", { target: field });
  assert.equal(sent.length, 2);
  assert.deepEqual(sent[1], { retention_days: 30 });
});

test("a delayed number save does not mark a newer input value as saved", async () => {
  sent.length = 0;
  let resolveFirst;
  responses = [new Promise((resolve) => { resolveFirst = resolve; }), ok];
  const field = input("number", "retention_days", "30", "7");
  const firstSave = form.fire("blur", { target: field });
  field.value = "31";
  resolveFirst(ok);
  await firstSave;
  await form.fire("blur", { target: field });
  assert.deepEqual(sent, [{ retention_days: 30 }, { retention_days: 31 }]);
});

test("a cleared number field is reported, not saved as its minimum", async () => {
  sent.length = 0;
  responses = [ok];
  const field = input("number", "smtp_port", "", "587");
  await form.fire("blur", { target: field });
  assert.equal(sent.length, 0);
  assert.equal(field.reported, true);
  assert.equal(field.value, "");
});

test("the SMTP password is saved with its surrounding whitespace", async () => {
  sent.length = 0;
  responses = [ok];
  const field = input("password", "smtp_password", " secret ", "");
  field.closest = (selector) => selector !== "[data-secret-field]" ? null : ({ querySelector: () => null, dataset: {}, classList: { toggle() {}, add() {}, remove() {} } });
  await form.fire("blur", { target: field });
  assert.deepEqual(sent, [{ smtp_password: " secret " }]);
});

test("the Signal enable switch is not saved by the generic autosave", async () => {
  sent.length = 0;
  responses = [ok];
  const field = { ...input("checkbox", "signal_enabled", "on", ""), checked: true };
  await form.fire("change", { target: field });
  assert.equal(sent.length, 0);
});
