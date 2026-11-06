//
// tests/js/htmx-confirm.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../../app/static/js/utils/htmx-confirm.js", import.meta.url), "utf8");

function setup({ confirmAnswer = true } = {}) {
  const listeners = {};
  const calls = [];
  const sandbox = {
    window: {
      confirmAction: async (options) => { calls.push(options); return confirmAnswer; },
    },
    document: { addEventListener: (type, fn) => { listeners[type] = fn; } },
    console,
  };
  vm.runInNewContext(source, sandbox);
  const element = (dataset) => ({ dataset, closest() { return this; } });
  const fire = async (dataset, issue) => {
    const event = { detail: { elt: element(dataset), issueRequest: issue }, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    await listeners["htmx:confirm"](event);
    await new Promise((resolve) => setImmediate(resolve));
    return event;
  };
  return { calls, fire, api: sandbox.window.JuHtmxConfirm };
}

const RESET = { juConfirm: "", confirmTitle: "Reset metrics", confirmMessage: "All metrics of 'Web' are deleted permanently.", confirmLabel: "Reset" };

test("a button without the data-ju-confirm flag is left to htmx", async () => {
  const { calls, fire } = setup();
  const event = await fire({}, () => assert.fail("must not be issued here"));
  assert.equal(event.defaultPrevented, false);
  assert.equal(calls.length, 0);
});

test("a flagged button asks the shared dialog with the texts of its data attributes", async () => {
  const { calls, fire } = setup();
  let issued = 0;
  const event = await fire(RESET, (skip) => { issued += 1; assert.equal(skip, true); });
  assert.equal(event.defaultPrevented, true);
  assert.deepEqual({ ...calls[0] }, { title: "Reset metrics", message: "All metrics of 'Web' are deleted permanently.", confirmLabel: "Reset" });
  assert.equal(issued, 1, "exactly one request after the confirmation");
});

test("cancelling sends nothing", async () => {
  const { fire } = setup({ confirmAnswer: false });
  const event = await fire(RESET, () => assert.fail("a cancelled dialog must not send the request"));
  assert.equal(event.defaultPrevented, true, "htmx must not continue on its own after Cancel");
});

test("a flagged button without a message never sends (fail closed)", async () => {
  const { calls, fire } = setup();
  const event = await fire({ juConfirm: "" }, () => assert.fail("no message, no request"));
  assert.equal(event.defaultPrevented, true, "htmx must not continue on its own without a message");
  assert.equal(calls.length, 0);
});

test("optionsFor falls back to neutral labels", () => {
  const { api } = setup();
  assert.deepEqual({ ...api.optionsFor({ confirmMessage: "m" }) }, { title: "Please confirm", message: "m", confirmLabel: "Confirm" });
  assert.equal(api.optionsFor({}), null);
});
