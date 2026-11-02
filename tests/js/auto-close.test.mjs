//
// tests/js/auto-close.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/target-detail/auto-close.js", import.meta.url), "utf8");

function harness(apiFetch) {
  const calls = [];
  const ctx = vm.createContext({
    console: { error() {} },
    apiFetch: async (url, opts) => { calls.push([url, opts]); return apiFetch(url, opts); },
  });
  vm.runInContext(src, ctx);
  return { calls, save: (toggle, id) => vm.runInContext("saveAutoCloseAlert", ctx)(toggle, id) };
}

test("success sends PATCH through apiFetch and keeps the toggle", async () => {
  const h = harness(() => ({ ok: true, status: 200 }));
  const toggle = { checked: true, disabled: false };
  await h.save(toggle, 7);
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0][0], "/targets/7");
  assert.equal(h.calls[0][1].method, "PATCH");
  assert.deepEqual(JSON.parse(h.calls[0][1].body), { auto_close_alert: true });
  assert.equal(toggle.checked, true);
});

test("403 reverts the toggle", async () => {
  const h = harness(() => ({ ok: false, status: 403 }));
  const toggle = { checked: true, disabled: false };
  await h.save(toggle, 7);
  assert.equal(toggle.checked, false);
});

test("network error reverts the toggle (off to on)", async () => {
  const h = harness(() => { throw new TypeError("offline"); });
  const toggle = { checked: false, disabled: false };
  await h.save(toggle, 7);
  assert.equal(toggle.checked, true);
});

test("disabled toggle sends nothing", async () => {
  const h = harness(() => ({ ok: true }));
  await h.save({ checked: true, disabled: true }, 7);
  assert.equal(h.calls.length, 0);
});

test("rapid changes are serialized and stale failures do not revert the latest choice", async () => {
  const replies = [];
  const h = harness(() => new Promise((resolve) => { replies.push(resolve); }));
  const toggle = { checked: true, disabled: false };
  const first = h.save(toggle, 7);
  toggle.checked = false;
  const second = h.save(toggle, 7);
  toggle.checked = true;
  const third = h.save(toggle, 7);

  await new Promise(setImmediate);
  assert.equal(h.calls.length, 1, "only the first target update is in flight");
  replies[0]({ ok: false, status: 500 });
  await first;
  assert.equal(toggle.checked, true, "an obsolete failure does not undo the latest choice");

  await new Promise(setImmediate);
  assert.equal(h.calls.length, 2);
  replies[1]({ ok: true, status: 200 });
  await second;
  await new Promise(setImmediate);
  assert.equal(h.calls.length, 3);
  replies[2]({ ok: true, status: 200 });
  await third;

  assert.deepEqual(h.calls.map(([, options]) => JSON.parse(options.body).auto_close_alert), [true, false, true]);
  assert.equal(toggle.checked, true);
});
