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
function harness(apiFetch, { confirmed = true, noDialog = false } = {}) {
  const calls = [];
  const dialogs = [];
  const root = el();
  root.dataset.targetId = "3";
  const parts = {
    preview: el(), confirm: el(), status: el(), error: el(),
    serviceGroup: el(), versionGroup: el(), serviceList: el(), versionList: el(),
  };
  const selectors = {
    '[data-service-action="preview"]': parts.preview,
    '[data-service-action="confirm"]': parts.confirm,
    "[data-service-status]": parts.status,
    "[data-service-error]": parts.error,
    '[data-service-group="service"]': parts.serviceGroup,
    '[data-service-group="version"]': parts.versionGroup,
    '[data-service-list="service"]': parts.serviceList,
    '[data-service-list="version"]': parts.versionList,
  };
  root.querySelector = (s) => selectors[s];
  const window = {
    location: { reload() { calls.push(["reload"]); } },
    apiFetch: async (url, opts) => { calls.push([url, JSON.parse(opts.body)]); return apiFetch(url); },
  };
  if (!noDialog) window.confirmAction = async (options) => { dialogs.push(options); return confirmed; };
  const document = {
    getElementById: () => root,
    createElement: () => ({ children: [], textContent: "" }),
  };
  vm.runInNewContext(src, { window, document, Number, String, JSON, Error });
  const requests = () => calls.filter(([url]) => url !== "reload");
  return { calls, dialogs, requests, ...parts };
}
const reply = (body, ok = true) => ({ ok, status: ok ? 200 : 409, json: async () => body });
const texts = (list) => list.children.map((item) => item.textContent);
const CHANGES = [
  { kind: "service_changed", protocol: "tcp", port: 25, previous_name: "smtp", previous_product: "Postfix", previous_version: "3.8", name: "http", product: "nginx", version: "1" },
  { kind: "identified", protocol: "udp", port: 53, previous_name: null, previous_product: null, previous_version: null, name: "domain", product: "BIND", version: "9" },
  { kind: "version_changed", protocol: "tcp", port: 22, previous_name: "ssh", previous_product: "OpenSSH", previous_version: "8.0", name: "ssh", product: "OpenSSH", version: "9.0" },
];
test("a preview without token keeps confirm hidden and reports no changes", async () => {
  const h = harness(() => reply({ token: null, changes: [] }));
  await h.preview.click();
  assert.equal(h.confirm.hidden, true);
  assert.equal(h.serviceGroup.hidden, true);
  assert.equal(h.versionGroup.hidden, true);
  assert.equal(h.status.textContent, "No changes");
});
test("service changes and version-only changes are listed in separate groups", async () => {
  const h = harness(() => reply({ token: "t1", changes: CHANGES }));
  await h.preview.click();
  assert.deepEqual(texts(h.serviceList), ["TCP:25 smtp / Postfix → http / nginx", "UDP:53 unknown → domain / BIND"]);
  assert.deepEqual(texts(h.versionList), ["TCP:22 ssh / OpenSSH 8.0 → 9.0"]);
  assert.equal(h.serviceGroup.hidden, false);
  assert.equal(h.versionGroup.hidden, false);
  assert.equal(h.confirm.hidden, false);
  assert.equal(h.status.textContent, "3 changes");
});
test("a version-only preview shows no service group", async () => {
  const h = harness(() => reply({ token: "t1", changes: [CHANGES[2]] }));
  await h.preview.click();
  assert.equal(h.serviceGroup.hidden, true);
  assert.equal(h.versionGroup.hidden, false);
  assert.equal(h.status.textContent, "1 change");
});
test("the preview alone never sends a confirmation", async () => {
  const h = harness(() => reply({ token: "t1", changes: CHANGES }));
  await h.preview.click();
  assert.deepEqual(h.requests().map(([url]) => url), ["/network-scans/services/3/preview"]);
  assert.equal(h.dialogs.length, 0);
});
test("confirm asks in the shared dialog first and sends the token only after Apply", async () => {
  const h = harness((url) => (url.endsWith("/preview") ? reply({ token: "t1", changes: CHANGES }) : reply([])));
  await h.preview.click();
  await h.confirm.click();
  assert.equal(h.dialogs.length, 1);
  assert.equal(h.dialogs[0].confirmLabel, "Apply");
  assert.match(h.dialogs[0].message, /3/);
  assert.deepEqual(h.requests().at(-1), ["/network-scans/services/3/confirm", { token: "t1" }]);
  assert.equal(h.calls.at(-1)[0], "reload");
});
test("cancelling the dialog sends nothing and keeps the preview confirmable", async () => {
  const h = harness(() => reply({ token: "t1", changes: CHANGES }), { confirmed: false });
  await h.preview.click();
  await h.confirm.click();
  assert.equal(h.requests().length, 1);
  assert.equal(h.confirm.hidden, false);
  assert.equal(h.serviceList.children.length, 2);
});
test("without the shared dialog nothing is applied", async () => {
  const h = harness(() => reply({ token: "t1", changes: CHANGES }), { noDialog: true });
  await h.preview.click();
  await h.confirm.click();
  assert.equal(h.requests().length, 1);
  assert.equal(h.confirm.hidden, false);
});
test("a failed confirm hides the button and cannot be replayed with the same token", async () => {
  const h = harness((url) => (url.endsWith("/preview")
    ? reply({ token: "t1", changes: [CHANGES[1]] })
    : reply({ detail: "expired" }, false)));
  await h.preview.click();
  assert.equal(h.confirm.hidden, false);
  await h.confirm.click();
  assert.equal(h.confirm.hidden, true);
  assert.equal(h.error.textContent, "expired");
  assert.equal(h.status.textContent, "");
  await h.confirm.click();
  assert.equal(h.requests().filter(([url]) => url.endsWith("/confirm")).length, 1);
});
test("a second preview click while one runs sends one request", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const h = harness(async () => { await gate; return reply({ token: null, changes: [] }); });
  const first = h.preview.click();
  const second = h.preview.click();
  release();
  await Promise.all([first, second]);
  assert.equal(h.requests().length, 1);
});
test("a new preview discards the previous token", async () => {
  const answers = [reply({ token: "t1", changes: CHANGES }), reply({ token: null, changes: [] })];
  const h = harness(() => answers.shift());
  await h.preview.click();
  await h.preview.click();
  assert.equal(h.confirm.hidden, true);
  await h.confirm.click();
  assert.equal(h.dialogs.length, 0);
  assert.equal(h.requests().filter(([url]) => url.endsWith("/confirm")).length, 0);
});
test("service names render as literal text without child elements", async () => {
  const evil = "<img src=x onerror=1>";
  const h = harness(() => reply({ token: "t", changes: [{ ...CHANGES[1], name: evil, product: null }] }));
  await h.preview.click();
  const item = h.serviceList.children[0];
  assert.equal(item.textContent, `UDP:53 unknown → ${evil}`);
  assert.equal(item.children.length, 0);
});
