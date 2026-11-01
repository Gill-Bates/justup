//
// tests/js/settings-tabs.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/utils/settings-tabs.js", import.meta.url), "utf8");
const { SettingsTabs } = (() => {
  const ctx = { globalThis: null };
  ctx.globalThis = ctx;
  vm.runInNewContext(src, ctx);
  return ctx;
})();

const IDS = ["general", "network", "storage", "spooler"];

test("parseHash normalizes case and the leading #", () => {
  assert.equal(SettingsTabs.parseHash("#Network"), "network");
  assert.equal(SettingsTabs.parseHash(""), "");
  assert.equal(SettingsTabs.parseHash(null), "");
});

test("a valid hash wins over the stored value", () => {
  assert.equal(SettingsTabs.resolveTabId("#storage", "network", IDS), "storage");
});

test("stored value is used only without a valid hash", () => {
  assert.equal(SettingsTabs.resolveTabId("", "network", IDS), "network");
  assert.equal(SettingsTabs.resolveTabId("#bogus", "network", IDS), "network");
});

test("unknown or missing values fall back to general", () => {
  assert.equal(SettingsTabs.resolveTabId("#bogus", "nope", IDS), "general");
  assert.equal(SettingsTabs.resolveTabId("", null, IDS), "general");
});

test("tabs that are not rendered fall back gracefully", () => {
  assert.equal(SettingsTabs.resolveTabId("#users", "users", IDS), "general");
  assert.equal(SettingsTabs.resolveTabId("#users", null, ["network"]), "network");
  assert.equal(SettingsTabs.resolveTabId("", null, []), null);
});
