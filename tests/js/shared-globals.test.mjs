//
// tests/js/shared-globals.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const root = new URL("../../app/", import.meta.url);
const read = (path) => readFileSync(new URL(path, root), "utf8");

function load(path, document = {}) {
  const sandbox = { document };
  sandbox.window = sandbox;
  vm.runInNewContext(read(path), sandbox);
  return sandbox;
}

test("getCsrfToken reads the csrf_token cookie on every call", () => {
  const document = { cookie: "a=1; csrf_token=abc%2Fdef; b=2" };
  const w = load("static/js/utils/csrf.js", document);
  assert.equal(w.getCsrfToken(), "abc/def");
  document.cookie = "csrf_token=rotated";
  assert.equal(w.getCsrfToken(), "rotated");
  document.cookie = "other=1; xcsrf_token=nope";
  assert.equal(w.getCsrfToken(), "");
  document.cookie = "";
  assert.equal(w.getCsrfToken(), "");
});

test("escapeHtml and escapeAttr neutralize markup and quotes and tolerate null", () => {
  const w = load("static/js/utils/escape.js");
  const out = w.escapeHtml("x\" onmouseover=\"alert(1)' <b>&");
  assert.doesNotMatch(out, /["'<>]/);
  assert.equal(out, "x&quot; onmouseover=&quot;alert(1)&#39; &lt;b&gt;&amp;");
  assert.equal(w.escapeAttr('"'), "&quot;");
  assert.equal(w.escapeHtml(null), "");
  assert.equal(w.escapeHtml(undefined), "");
  assert.equal(w.escapeHtml(0), "0");
});

function jsFiles(dir = "static/js") {
  const found = [];
  for (const entry of readdirSync(new URL(dir + "/", root), { withFileTypes: true })) {
    const path = `${dir}/${entry.name}`;
    if (entry.isDirectory()) found.push(...jsFiles(path));
    else if (entry.name.endsWith(".js") && !entry.name.includes(".min.") && !entry.name.includes("bundle")) found.push(path);
  }
  return found;
}

test("every first-party script using the shared globals can resolve them", () => {
  const defining = new Set(["static/js/utils/csrf.js", "static/js/utils/escape.js"]);
  const users = jsFiles().filter((file) => !defining.has(file)
    && /\bgetCsrfToken\s*\(|\bescapeHtml\s*\(|\bescapeAttr\s*\(/.test(read(file)));
  assert.ok(users.length > 0);
  // login.js, change-password.js and the base layout pages are the entry points for these scripts.
  for (const page of ["templates/base.html", "templates/login.html", "templates/change_password.html"]) {
    const html = read(page);
    const csrf = html.indexOf("/static/js/utils/csrf.js");
    const escape = html.indexOf("/static/js/utils/escape.js");
    assert.ok(csrf > -1 && escape > -1, `${page} must include csrf.js and escape.js`);
    const scripts = [...html.matchAll(/<script src="\/static\/js\/([^"]+)"/g)].map((m) => ({ name: m[1], at: m.index }));
    for (const script of scripts) {
      if (users.includes(`static/js/${script.name}`)) {
        assert.ok(script.at > csrf && script.at > escape, `${page}: ${script.name} must load after the shared utils`);
      }
    }
  }
  const login = read("templates/login.html");
  assert.match(login, /utils\/csrf\.js[\s\S]*login\.js/);
  assert.match(read("templates/change_password.html"), /utils\/csrf\.js[\s\S]*change-password\.js/);
});

test("no first-party script redefines the shared helpers differently", () => {
  for (const file of jsFiles()) {
    if (file.startsWith("static/js/utils/")) continue;
    assert.doesNotMatch(read(file), /function\s+getCsrfToken\s*\(/, file);
  }
});
