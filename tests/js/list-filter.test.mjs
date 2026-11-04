//
// tests/js/list-filter.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../../app/static/js/utils/list-filter.js", import.meta.url), "utf8");
const sandbox = {};
sandbox.window = sandbox;
vm.runInNewContext(source, sandbox);
const { normalize, normalizeQuery, matches, filterRows, statusMessage, countLabel, createExpansionMemory, MAX_QUERY_LENGTH } = sandbox.JuListFilter;
// Values built inside the vm context are not reference-equal to ours; compare plain copies.
const plain = (value) => JSON.parse(JSON.stringify(value));

test("normalize lower-cases, trims and collapses whitespace", () => {
  assert.equal(normalize("  Web   SITE \t\n"), "web site");
  assert.equal(normalize(null), "");
  assert.equal(normalize(undefined), "");
  assert.equal(normalize(42), "42");
});

test("diacritics fold both ways, so an umlaut finds its base letter and vice versa", () => {
  assert.equal(normalize("Lüttringhausen"), "luttringhausen");
  assert.ok(matches("efg-Lüttringhausen.de", normalizeQuery("lüttring")));
  assert.ok(matches("efg-Luttringhausen.de", normalizeQuery("LÜTTRING")));
  assert.ok(matches("Café Zürich", normalizeQuery("cafe zurich")));
});

test("an empty or whitespace-only query matches everything", () => {
  assert.equal(normalizeQuery(""), "");
  assert.equal(normalizeQuery("   \t "), "");
  assert.ok(matches("anything", normalizeQuery("   ")));
  assert.ok(matches("", ""));
});

test("matching is a case-insensitive substring test", () => {
  assert.ok(matches("Example Web example.com", normalizeQuery("MPLE we")));
  assert.ok(matches("example.com", normalizeQuery("example.com")));
  assert.ok(!matches("example.com", normalizeQuery("example.org")));
});

test("regular-expression characters are literal", () => {
  for (const query of [".", "(", ")", "[", "*", "+", "?", "^", "$", "\\", "|", "a.c", "(unclosed", "[a-z]"]) {
    assert.doesNotThrow(() => matches("abc example.com", normalizeQuery(query)), query);
  }
  assert.ok(!matches("abc", normalizeQuery("a.c")), "'.' does not match any character");
  assert.ok(matches("a.c", normalizeQuery("a.c")));
  assert.ok(matches("f(x)", normalizeQuery("(x")));
  assert.ok(!matches("example.com", normalizeQuery(".*")));
  assert.ok(matches("a*b", normalizeQuery("a*b")));
});

test("a very long query is capped and never matches a real field", () => {
  const long = "a".repeat(50000);
  assert.equal(normalizeQuery(long).length, MAX_QUERY_LENGTH);
  assert.ok(!matches("short name", normalizeQuery(long)));
});

test("result messages", () => {
  assert.equal(statusMessage(0), "No targets found");
  assert.equal(statusMessage(1), "1 target found");
  assert.equal(statusMessage(3), "3 targets found");
  assert.equal(countLabel(2, 5), "2 / 5");
  assert.equal(statusMessage(0, "recipient"), "No recipients found");
  assert.equal(statusMessage(1, "recipient"), "1 recipient found");
  assert.equal(statusMessage(2, "recipient"), "2 recipients found");
});

test("filterRows hides rows without a hit and returns the hit count (shared by Targets and Recipients)", () => {
  const rows = ["Martin martin@example.org", "Tobias", "MARTINA"].map((search) => ({ dataset: { search }, hidden: false }));
  assert.equal(filterRows(rows, normalizeQuery(" martin ")), 2);
  assert.deepEqual(rows.map((row) => row.hidden), [false, true, false]);
  assert.equal(filterRows(rows, normalizeQuery("zzz")), 0);
  assert.ok(rows.every((row) => row.hidden));
  assert.equal(filterRows(rows, ""), 3);
  assert.ok(rows.every((row) => !row.hidden));
});

test("expansion memory returns the state from before the search, first value wins", () => {
  const memory = createExpansionMemory();
  memory.remember("group-a", false);
  memory.remember("group-a", true); // already opened by the search: must not overwrite
  memory.remember("group-b", true);
  assert.deepEqual(plain(memory.take()), [["group-a", false], ["group-b", true]]);
  assert.deepEqual(plain(memory.take()), [], "taking empties the memory");
});

test("a manual toggle during the search replaces the remembered state of that group only", () => {
  const memory = createExpansionMemory();
  memory.remember("group-a", false);
  memory.override("group-a", true);
  memory.override("group-unknown", true); // never remembered: nothing to restore
  assert.deepEqual(plain(memory.take()), [["group-a", true]]);
});

test("clear forgets everything (fragment replaced during the search)", () => {
  const memory = createExpansionMemory();
  memory.remember("group-a", true);
  memory.clear();
  assert.deepEqual(plain(memory.take()), []);
});
