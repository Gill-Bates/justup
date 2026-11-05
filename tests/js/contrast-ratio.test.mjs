//
// tests/js/contrast-ratio.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import test from "node:test";
import { contrastRatio } from "../layout/fixtures.mjs";

// Contract: WCAG 2 relative-luminance contrast of two computed CSS colours, order independent.
test("contrastRatio gives 21 for black on white in either order", () => {
  assert.equal(contrastRatio("rgb(0, 0, 0)", "rgb(255, 255, 255)"), 21);
  assert.equal(contrastRatio("rgb(255, 255, 255)", "rgb(0, 0, 0)"), 21);
});

test("contrastRatio gives 1 for equal colours", () => {
  assert.equal(contrastRatio("rgb(95, 111, 128)", "rgb(95, 111, 128)"), 1);
});

test("contrastRatio parses rgba and space-separated syntax", () => {
  assert.equal(contrastRatio("rgba(0, 0, 0, 0.5)", "rgb(255 255 255)"), 21);
  assert.equal(contrastRatio("rgb(0 0 0 / 50%)", "rgba(255,255,255,1)"), 21);
});

test("contrastRatio keeps the measured value for #5f6f80 on #f7f7f7", () => {
  assert.ok(Math.abs(contrastRatio("rgb(95, 111, 128)", "rgb(247, 247, 247)") - 4.813835977529256) < 1e-9);
});

test("contrastRatio rejects an unparsable colour", () => {
  assert.throws(() => contrastRatio("transparent", "rgb(0, 0, 0)"), /unparsable colour/);
});
