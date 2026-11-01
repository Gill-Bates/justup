//
// tests/js/tag-input-core.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import test from "node:test";
import {
  addTokens, evaluate, initialTokens, isValidEntry, removeToken, sameList, serialize, splitTokens,
} from "../../app/static/js/utils/tag-input-core.js";

const DEFAULTS = ["https://1.1.1.1", "8.8.8.8", "9.9.9.9", "https://www.de-cix.net", "https://cloudflare.com"];
// Must mirror QUALITY_TARGET_PATTERN in app/utils/runtime_fields.py.
const OPTS = {
  defaults: DEFAULTS, min: 3, maxLength: 300, maxTotal: 2000,
  pattern: "^(https?://)?[A-Za-z0-9][A-Za-z0-9.:\\[\\]-]{0,252}(/[^\\s]*)?$",
};

test("splitTokens handles commas, newlines and surrounding whitespace", () => {
  assert.deepEqual(splitTokens(" a.example, b.example\nc.example\r\n,, "), ["a.example", "b.example", "c.example"]);
  assert.deepEqual(splitTokens("a b.example"), ["a b.example"]);
  assert.deepEqual(splitTokens(""), []);
  assert.deepEqual(splitTokens(null), []);
});

test("addTokens dedupes case-insensitively and reports duplicates", () => {
  const r = addTokens(["Example.org"], "example.org, new.example,NEW.example");
  assert.deepEqual(r.list, ["Example.org", "new.example"]);
  assert.deepEqual(r.added, ["new.example"]);
  assert.deepEqual(r.duplicates, ["example.org", "NEW.example"]);
});

test("removeToken removes by index without mutating", () => {
  const list = ["a", "b", "c"];
  assert.deepEqual(removeToken(list, 1), ["a", "c"]);
  assert.deepEqual(list, ["a", "b", "c"]);
});

test("initialTokens falls back to defaults only for an empty stored value", () => {
  assert.deepEqual(initialTokens("", DEFAULTS), DEFAULTS);
  assert.deepEqual(initialTokens("  ", DEFAULTS), DEFAULTS);
  assert.deepEqual(initialTokens("a.example,b.example,c.example", DEFAULTS), ["a.example", "b.example", "c.example"]);
});

test("serialize sends an empty string for unchanged defaults, a list otherwise", () => {
  assert.equal(serialize(DEFAULTS.slice(), DEFAULTS), "");
  assert.equal(serialize(DEFAULTS.map((d) => d.toUpperCase()), DEFAULTS), "");
  assert.equal(serialize([], DEFAULTS), "");
  assert.equal(serialize(["a.example", "b.example", "c.example"], DEFAULTS), "a.example,b.example,c.example");
  assert.equal(serialize(DEFAULTS.slice(0, 4), DEFAULTS), DEFAULTS.slice(0, 4).join(","));
});

test("isValidEntry mirrors the backend rule", () => {
  for (const ok of ["8.8.8.8", "https://1.1.1.1", "dns.google", "http://[::1]:8080/x", "host.example/path?q=1"]) {
    assert.equal(isValidEntry(ok, OPTS), true, ok);
  }
  for (const bad of ["bad host!", "-x.example", "a b", "a".repeat(301)]) {
    assert.equal(isValidEntry(bad, OPTS), false, bad);
  }
});

test("evaluate enforces the minimum count but allows an empty list", () => {
  assert.equal(evaluate([], OPTS).valid, true);
  assert.equal(evaluate([], OPTS).usingDefaults, true);
  const two = evaluate(["a.example", "b.example"], OPTS);
  assert.equal(two.valid, false);
  assert.equal(two.belowMin, true);
  assert.match(two.message, /At least 3/);
  assert.equal(evaluate(["a.example", "b.example", "c.example"], OPTS).valid, true);
});

test("evaluate flags invalid entries and oversize totals", () => {
  const bad = evaluate(["a.example", "b.example", "bad host!"], OPTS);
  assert.equal(bad.valid, false);
  assert.deepEqual(bad.invalidEntries, ["bad host!"]);
  const long = Array.from({ length: 20 }, (_, i) => `h${i}.${"x".repeat(150)}.example`);
  assert.match(evaluate(long, OPTS).message, /too long/);
});

test("removing the last badge falls back to the built-in defaults", () => {
  assert.deepEqual(removeToken(["a.example"], 0, DEFAULTS), DEFAULTS);
  assert.deepEqual(removeToken(["a.example", "b.example"], 0, DEFAULTS), ["b.example"]);
  assert.deepEqual(removeToken(["a.example"], 0, []), []);
});

test("stored value and displayed badges stay in sync across a reload or tab switch", () => {
  const edited = ["a.example", "b.example", "c.example", "d.example"];
  const wire = serialize(edited, DEFAULTS);
  assert.deepEqual(initialTokens(wire, DEFAULTS), edited);
  assert.deepEqual(initialTokens(serialize(DEFAULTS, DEFAULTS), DEFAULTS), DEFAULTS);
});

test("an invalid token is kept visible and blocks saving, a correction unblocks it", () => {
  const typed = addTokens(["a.example", "b.example", "c.example"], "bad host!");
  assert.deepEqual(typed.added, ["bad host!"]);
  assert.equal(evaluate(typed.list, OPTS).valid, false);
  const fixed = removeToken(typed.list, 3, DEFAULTS);
  assert.equal(evaluate(fixed, OPTS).valid, true);
  assert.equal(evaluate(fixed, OPTS).serialized, "a.example,b.example,c.example");
});

test("an unchanged list reports sameList so no redundant save is triggered", () => {
  assert.equal(sameList(["A.example", "b.example"], ["a.example", "B.example"]), true);
  assert.equal(sameList(["a.example"], ["a.example", "b.example"]), false);
});
