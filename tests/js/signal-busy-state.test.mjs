//
// tests/js/signal-busy-state.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
import assert from "node:assert/strict";
import test from "node:test";
function makeEl() {
  return {
    style: {}, className: "", innerHTML: "", textContent: "", value: "", disabled: false, checked: false,
    dataset: {},
    classList: { add() {}, remove() {}, contains: () => false },
    children: [],
    replaceChildren() { this.children = []; this.innerHTML = ""; },
    append(...n) { this.children.push(...n); },
    appendChild(n) { this.children.push(n); },
    querySelector: () => null,
    querySelectorAll: () => [],
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
globalThis.justUpToast = { success() {}, error() {}, warning() {}, info() {} };
let respond;
globalThis.window.apiFetch = async (url, opts = {}) => respond(url, opts);
const core = await import("../../app/static/js/settings.signal-core.js");
const registration = await import("../../app/static/js/settings.signal-registration.js");
const actions = await import("../../app/static/js/settings.signal-actions.js");
const BUTTON_IDS = [
  "signal-link-confirm-btn", "signal-register-btn", "signal-verify-btn",
  "confirm-unlink-device-btn", "confirm-unregister-phone-btn", "send-test-btn",
];
const buttons = () => BUTTON_IDS.map(id => document.getElementById(id));
const json = (status, body) => ({ ok: status < 400, status, json: async () => body, text: async () => JSON.stringify(body) });
function reset() {
  for (const b of buttons()) b.disabled = false;
  registration.setStatusLoader(() => {});
}
test("leaving the busy state re-enables only the buttons that busy disabled", () => {
  reset();
  document.getElementById("signal-verify-btn").disabled = true;
  core.setSignalBusy(true);
  assert.ok(buttons().every(b => b.disabled));
  core.setSignalBusy(false);
  assert.equal(core.isSignalBusy(), false);
  for (const b of buttons()) {
    assert.equal(b.disabled, b === document.getElementById("signal-verify-btn"));
  }
});
test("a nested busy request does not capture buttons it disabled itself", () => {
  reset();
  core.setSignalBusy(true);
  core.setSignalBusy(true);
  core.setSignalBusy(false);
  assert.ok(buttons().every(b => !b.disabled));
});
for (const [name, handler] of [
  ["503", () => json(503, { detail: "down" })],
  ["200", () => json(200, {})],
  ["network error", () => { throw new TypeError("offline"); }],
]) {
  test(`registration (${name}) leaves the action buttons usable`, async () => {
    reset();
    document.getElementById("signal-register-number").value = "+491701234567";
    respond = handler;
    await registration.signalRegister();
    assert.ok(buttons().every(b => !b.disabled));
  });
}
test("verify leaves the action buttons usable after success and failure", async () => {
  reset();
  document.getElementById("signal-register-number").value = "+491701234567";
  document.getElementById("signal-verify-code").value = "123456";
  for (const r of [json(400, { detail: "bad code" }), json(200, {})]) {
    respond = () => r;
    await registration.signalVerify();
    assert.ok(buttons().every(b => !b.disabled));
  }
});
test("QR link confirmation leaves the action buttons usable", async () => {
  reset();
  respond = () => json(504, { detail: "Timeout" });
  await registration.signalConfirmLink();
  assert.ok(buttons().every(b => !b.disabled));
  respond = () => json(200, { accounts: [] });
  await registration.signalConfirmLink();
  assert.ok(buttons().every(b => !b.disabled));
});
test("unregister leaves the action buttons usable after success and failure", async () => {
  reset();
  document.getElementById("unregister-phone-password").value = "pw";
  for (const r of [json(401, { detail: "Invalid password" }), json(200, {})]) {
    respond = () => r;
    await actions.unregisterPhone();
    assert.ok(buttons().every(b => !b.disabled));
  }
});
