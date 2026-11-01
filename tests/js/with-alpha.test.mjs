//
// tests/js/with-alpha.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const src = readFileSync(new URL("../../app/static/js/utils/with-alpha.js", import.meta.url), "utf8");
const ctx = vm.createContext({});
vm.runInContext(src, ctx);
const withAlpha = vm.runInContext("withAlpha", ctx);

test("withAlpha keeps supporting hex and rgb colors", () => {
  assert.equal(withAlpha("#0d6efd", 0.5), "rgba(13, 110, 253, 0.5)");
  assert.equal(withAlpha("#fff", 0.1), "rgba(255, 255, 255, 0.1)");
  assert.equal(withAlpha("rgb(1, 2, 3)", 0.2), "rgba(1, 2, 3, 0.2)");
});

test("withAlpha applies alpha to hsl() in comma and space syntax", () => {
  assert.equal(withAlpha("hsl(210, 70%, 45%)", 0.12), "hsla(210, 70%, 45%, 0.12)");
  assert.equal(withAlpha("hsl(210 70% 45%)", 0.12), "hsla(210, 70%, 45%, 0.12)");
  assert.equal(withAlpha("hsla(10.5, 70%, 65%, 1)", 0.3), "hsla(10.5, 70%, 65%, 0.3)");
});

test("withAlpha returns unknown formats unchanged", () => {
  assert.equal(withAlpha("tomato", 0.5), "tomato");
});
