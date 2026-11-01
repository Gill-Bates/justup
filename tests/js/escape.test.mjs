//
// tests/js/escape.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Contract: the shipped theme bootstrap script must not use inline eval-like constructs (CSP, NFR-10).
test("theme-init.js avoids eval and Function constructors", () => {
  const src = readFileSync(new URL("../../app/static/js/theme-init.js", import.meta.url), "utf8");
  assert.doesNotMatch(src, /\beval\s*\(|new\s+Function\s*\(/);
});
