//
// tests/js/signal-register-errors.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import test from "node:test";
function makeEl() {
  const classes = new Set();
  return {
    style: {}, className: "", textContent: "", value: "", disabled: false, checked: false, dataset: {},
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle() {}, has: c => classes.has(c),
    },
    children: [],
    replaceChildren(...nodes) { this.children = nodes; this.textContent = nodes.map(n => n.textContent || "").join(""); },
    append(...nodes) { this.children.push(...nodes); this.textContent += nodes.map(n => n.textContent || "").join(""); },
    appendChild(node) { this.children.push(node); this.textContent += node.textContent || ""; },
    querySelector: () => null, querySelectorAll: () => [],
    setAttribute() {}, removeAttribute() {}, addEventListener() {}, focus() {},
  };
}
const els = {};
globalThis.document = {
  getElementById: id => (els[id] ??= makeEl()),
  createElement: () => makeEl(),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
};
globalThis.window = globalThis;
const toasts = [];
globalThis.justUpToast = {
  success: m => toasts.push(["success", m]), error: m => toasts.push(["error", m]),
  warning: m => toasts.push(["warning", m]), info: m => toasts.push(["info", m]),
};
let respond;
globalThis.window.apiFetch = async () => respond();
await import("../../app/static/js/utils/warning.js");
await import("../../app/static/js/utils/api-error.js");
const registration = await import("../../app/static/js/settings.signal-registration.js");
let statusReloads = 0;
const json = (status, body) => ({ ok: status < 400, status, json: async () => body, text: async () => JSON.stringify(body) });
const el = id => document.getElementById(id);

async function register(response) {
  el("signal-register-number").value = "+491701234567";
  el("signal-captcha-section").classList.add("d-none");
  toasts.length = 0;
  statusReloads = 0;
  registration.setStatusLoader(() => { statusReloads += 1; });
  respond = () => response;
  await registration.signalRegister();
}

test("a captcha_required code reveals the captcha section whatever the message says", async () => {
  await register(json(400, { detail: { code: "captcha_required", message: "reworded text" } }));
  assert.equal(el("signal-captcha-section").classList.contains("d-none"), false);
  assert.match(el("signal-register-status").textContent, /Captcha required/);
});

test("an already_registered code notifies and reloads the status whatever the message says", async () => {
  await register(json(400, { detail: { code: "already_registered", message: "reworded text" } }));
  assert.deepEqual(toasts, [["info", "This number is already registered"]]);
  assert.equal(statusReloads, 1);
});

test("an unknown error shows its message and ignores keywords in the text", async () => {
  await register(json(400, { detail: "Signal registration failed; captcha already registered" }));
  assert.equal(el("signal-captcha-section").classList.contains("d-none"), true);
  assert.equal(statusReloads, 0);
  assert.match(el("signal-register-status").textContent, /Signal registration failed/);
});

test("an unknown coded error is shown as an error with its message", async () => {
  await register(json(400, { detail: { code: "other", message: "Something else" } }));
  assert.equal(el("signal-captcha-section").classList.contains("d-none"), true);
  assert.match(el("signal-register-status").textContent, /Something else/);
});
