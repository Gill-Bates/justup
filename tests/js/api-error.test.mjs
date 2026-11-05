//
// tests/js/api-error.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../../app/static/js/utils/api-error.js", import.meta.url), "utf8");
const sandbox = {};
sandbox.window = sandbox;
vm.runInNewContext(source, sandbox);
const { message, code } = sandbox.JuApiError;

test("a string detail is shown as it is", () => {
  assert.equal(message("Username already exists", "fallback"), "Username already exists");
});

test("a {code, message} detail shows its message and exposes its code", () => {
  const detail = { code: "captcha_required", message: "Captcha required" };
  assert.equal(message(detail, "fallback"), "Captcha required");
  assert.equal(code(detail), "captcha_required");
  assert.equal(code("plain text"), null);
  assert.equal(code([{ msg: "x" }]), null);
});

test("a 422 list is joined into readable text, never [object Object]", () => {
  assert.equal(message([{ loc: ["body", "to"], msg: "value is not a valid email address" }], "f"), "value is not a valid email address");
  assert.equal(message([{ msg: "first" }, { msg: "second" }], "f"), "first; second");
  assert.equal(message([{ msg: "kept" }, { loc: ["x"] }, null], "f"), "kept");
});

test("nothing readable falls back to the caller's text", () => {
  for (const empty of [null, undefined, 42, "", "   ", [], [{ loc: ["x"] }], {}, { message: "" }]) {
    assert.equal(message(empty, "Request failed"), "Request failed");
  }
  assert.equal(message(null), "");
});
