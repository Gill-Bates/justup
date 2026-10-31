//
// tests/js/signal-code-boxes.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
import assert from "node:assert/strict";
import test from "node:test";

function box() {
  const listeners = {};
  return {
    value: "", disabled: false, classList: { add() {}, remove() {} },
    addEventListener(type, fn) { (listeners[type] ??= []).push(fn); },
    count: (type) => (listeners[type] ?? []).length,
    focus() {}, select() {}, removeAttribute() {}, setAttribute() {},
  };
}
function makeContainer() {
  const boxes = Array.from({ length: 6 }, box);
  return { boxes, querySelectorAll: () => boxes };
}
function el() {
  return {
    style: {}, className: "", innerHTML: "", textContent: "", value: "", disabled: false, checked: false,
    dataset: {}, children: [],
    classList: { add() {}, remove() {}, contains: () => false },
    replaceChildren() { this.children = []; this.innerHTML = ""; },
    append(...n) { this.children.push(...n); },
    appendChild(n) { this.children.push(n); },
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {}, setAttribute() {}, removeAttribute() {}, focus() {},
  };
}
let container = makeContainer();
const steps = {};
globalThis.document = {
  getElementById: (id) => {
    if (id === "signal-verify-boxes") return container;
    return (steps[id] ??= el());
  },
  createElement: () => el(),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
};
globalThis.window = globalThis;
globalThis.justUpToast = { success() {}, error() {}, warning() {}, info() {} };
globalThis.setTimeout = () => 0;
globalThis.window.apiFetch = async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => "{}" });

const core = await import("../../app/static/js/settings.core.js");
const registration = await import("../../app/static/js/settings.signal-registration.js");

test("code boxes get listeners once per container, again after the fragment is replaced", async () => {
  const advance = async () => {
    document.getElementById("signal-register-number").value = "+491701234567";
    await registration.signalRegister();
  };
  await advance();
  await advance();
  assert.ok(container.boxes.every((b) => b.count("input") === 1));
  const first = container;
  container = makeContainer();
  core.resetDomCache();
  await advance();
  assert.ok(container.boxes.every((b) => b.count("input") === 1));
  assert.ok(first.boxes.every((b) => b.count("input") === 1));
});
