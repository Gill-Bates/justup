//
// tests/js/link-url.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../../app/static/js/utils/link-url.js", import.meta.url), "utf8");
const sandbox = {};
sandbox.window = sandbox;
vm.runInNewContext(source, sandbox);
const { validateLinkUrl } = sandbox.JuLinkUrl;
const plain = (value) => JSON.parse(JSON.stringify(value));

test("web and mail addresses are accepted and trimmed", () => {
  assert.deepEqual(plain(validateLinkUrl("  https://example.org/a?b=1 ")), { ok: true, url: "https://example.org/a?b=1" });
  assert.equal(validateLinkUrl("http://example.org").ok, true);
  assert.equal(validateLinkUrl("mailto:ops@example.org").ok, true);
});

test("an empty value or the bare prefix is an error with a message", () => {
  for (const empty of ["", "   ", null, undefined, "https://"]) {
    const result = validateLinkUrl(empty);
    assert.equal(result.ok, false);
    assert.match(result.error, /web address/i);
  }
});

test("script-capable schemes are refused, whatever the case or hidden characters", () => {
  for (const bad of ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "java\tscript:alert(1)", " \u0001javascript:x", "data:text/html,<b>x", "vbscript:x"]) {
    const result = validateLinkUrl(bad);
    assert.equal(result.ok, false, bad);
    assert.match(result.error, /not allowed/i);
  }
});
test("the template sanitiser's scheme check refuses the same schemes as the link dialog", () => {
  const { isScriptUrl } = sandbox.JuLinkUrl;
  for (const bad of ["javascript:alert(1)", "JAVASCRIPT:x", "java\tscript:x", "data:text/html,<b>x", "vbscript:x"]) {
    assert.equal(isScriptUrl(bad), true, bad);
  }
  for (const ok of ["https://example.org", "mailto:ops@example.org", "/relative", "", null]) {
    assert.equal(isScriptUrl(ok), false, String(ok));
  }
});
