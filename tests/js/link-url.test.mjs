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
test("the template sanitiser keeps inline raster images and still blocks active content", () => {
  const { isBlockedUrlAttribute } = sandbox.JuLinkUrl;
  const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==";
  for (const ok of [png, png.toUpperCase().replace("DATA:IMAGE/PNG", "data:image/png"), "data:image/jpeg;base64,/9j/4AAQ", "data:image/gif;base64,R0lGOD"]) {
    assert.equal(isBlockedUrlAttribute("IMG", "src", ok), false, ok);
  }
  const bad = ["javascript:alert(1)", "vbscript:x", "data:text/html;base64,PGI+", "data:image/svg+xml;base64,PHN2Zz4=",
    "data:image/svg+xml,<svg onload=alert(1)>", "data:image/png,notbase64", "data:image/png;base64,AA\"onerror=x", "data:application/javascript,1"];
  for (const value of bad) assert.equal(isBlockedUrlAttribute("img", "src", value), true, value);
  // Raster data URLs stay blocked wherever they are not an image source.
  assert.equal(isBlockedUrlAttribute("a", "href", png), true);
  assert.equal(isBlockedUrlAttribute("img", "href", png), true);
  assert.equal(isBlockedUrlAttribute("iframe", "src", png), true);
  assert.equal(isBlockedUrlAttribute("img", "src", "https://example.org/a.png"), false);
});
