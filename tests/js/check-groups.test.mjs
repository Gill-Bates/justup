//
// tests/js/check-groups.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
const source = readFileSync(new URL("../../app/static/js/utils/check-groups.js", import.meta.url), "utf8");
const optionalGroupsSource = readFileSync(new URL("../../app/static/js/utils/optional-groups.js", import.meta.url), "utf8");
const sandbox = {};
sandbox.window = sandbox;
vm.runInNewContext(optionalGroupsSource, sandbox);
vm.runInNewContext(source, sandbox);
const { visibleGroups, apply } = sandbox.JuCheckGroups;
test("each of the 32 toggle combinations shows exactly the groups of its active checks", () => {
  for (let mask = 0; mask < 32; mask += 1) {
    const checks = { http: Boolean(mask & 1), ping: Boolean(mask & 2), tcp: Boolean(mask & 4), cert: Boolean(mask & 8), udp: Boolean(mask & 16) };
    const groups = visibleGroups(checks);
    assert.equal(groups.http, checks.http, `http ${mask}`);
    assert.equal(groups.ping, checks.ping, `ping ${mask}`);
    assert.equal(groups.tcp, checks.tcp, `tcp ${mask}`);
    assert.equal(groups.udp, checks.udp, `udp ${mask}`);
    assert.equal(groups.cert, checks.cert, `cert ${mask}`);
    assert.equal(groups.tls, checks.http || checks.cert, `tls ${mask}`);
  }
});
test("a TCP-only target shows no HTTP, TLS or UDP group, and UDP does not need TCP", () => {
  assert.deepEqual({ ...visibleGroups({ tcp: true }) }, { http: false, tls: false, tcp: true, udp: false, ping: false, cert: false });
  assert.deepEqual({ ...visibleGroups({ udp: true }) }, { http: false, tls: false, tcp: false, udp: true, ping: false, cert: false });
});
test("missing input shows nothing", () => {
  assert.deepEqual({ ...visibleGroups(undefined) }, { http: false, tls: false, tcp: false, udp: false, ping: false, cert: false });
});
test("apply toggles hidden on every element of a group", () => {
  const make = (checkGroup) => ({ dataset: { checkGroup }, hidden: false });
  const elements = [make("http"), make("tcp"), make("tls"), make("http")];
  apply({ querySelectorAll: () => elements }, { tcp: true });
  assert.deepEqual(elements.map((element) => element.hidden), [true, false, true, true]);
  apply({ querySelectorAll: () => elements }, { http: true });
  assert.deepEqual(elements.map((element) => element.hidden), [false, true, false, false]);
});
