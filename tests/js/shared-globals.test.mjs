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

function load(path, document = {}, globals = {}) {
  const sandbox = { document, ...globals };
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

test("apiFetch adds the current CSRF token and same-origin credentials", async () => {
  const calls = [];
  const w = load("static/js/app.js", {
    cookie: "csrf_token=token%2Fvalue",
    readyState: "loading",
    addEventListener() {},
  }, {
    Headers,
    getCsrfToken: () => "token/value",
    withCsrf: load("static/js/utils/csrf.js", { cookie: "csrf_token=token%2Fvalue" }, { Headers }).withCsrf,
    fetch: async (...args) => { calls.push(args); return { ok: true }; },
  });

  await w.apiFetch("/mutate", { method: "POST", headers: { Accept: "application/json" } });
  assert.equal(calls[0][1].headers.get("X-CSRF-Token"), "token/value");
  assert.equal(calls[0][1].credentials, "same-origin");
  assert.equal(calls[0][1].headers.get("Accept"), "application/json");

  await w.apiFetch("/explicit", { method: "POST", headers: { "X-CSRF-Token": "caller-token" } });
  assert.equal(calls[1][1].headers.get("X-CSRF-Token"), "caller-token");
});

test("withCsrf keeps caller headers and credentials, tolerates a missing cookie and always sends on GET", () => {
  const { withCsrf } = load("static/js/utils/csrf.js", { cookie: "csrf_token=t1" }, { Headers });
  const get = withCsrf({ method: "GET" });
  assert.equal(get.headers.get("X-CSRF-Token"), "t1");
  assert.equal(get.credentials, "same-origin");
  assert.equal(withCsrf({ credentials: "include" }).credentials, "include");
  assert.equal(withCsrf().headers.get("X-CSRF-Token"), "t1");
  const bare = load("static/js/utils/csrf.js", { cookie: "" }, { Headers }).withCsrf({ method: "POST" });
  assert.equal(bare.headers.has("X-CSRF-Token"), false);
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

test("parseUtcTimestamp reads offset-less timestamps as UTC and keeps explicit offsets", () => {
  const { parseUtcTimestamp } = load("static/js/utils/utc-time.js");
  const expected = Date.parse("2026-11-01T12:00:00Z");
  assert.equal(parseUtcTimestamp("2026-11-01 12:00:00"), expected);
  assert.equal(parseUtcTimestamp("2026-11-01T12:00:00"), expected);
  assert.equal(parseUtcTimestamp("2026-11-01T14:00:00+02:00"), expected);
  assert.ok(Number.isNaN(parseUtcTimestamp("garbage")));
});
