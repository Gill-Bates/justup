//
// tests/js/port-scan-core.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
const source = readFileSync(new URL("../../app/static/js/utils/port-scan-core.js", import.meta.url), "utf8");
const sandbox = {};
sandbox.window = sandbox;
vm.runInNewContext(source, sandbox);
const core = sandbox.JuPortScan;
const plain = value => JSON.parse(JSON.stringify(value));
const job = (status, extra = {}) => ({ id: "j1", status, ...extra });
const result = ports => ({ hosts: [{ address: "192.0.2.1", status: "up", ports }] });
test("a port list is validated strictly and keeps invalid parts visible", () => {
  assert.deepEqual(plain(core.parsePortList("443, 22,22")), { ports: [22, 443], invalid: [] });
  assert.deepEqual(plain(core.parsePortList("22,80abc,0,65536,1-5")).invalid, ["80abc", "0", "65536", "1-5"]);
  assert.deepEqual(plain(core.parsePortList("")), { ports: [], invalid: [] });
  assert.equal(core.parsePort("65535"), 65535);
  assert.equal(core.parsePort("65536"), null);
  assert.equal(core.parsePort("1e3"), null);
});
test("typed manual input separates TCP and UDP and defaults to TCP", () => {
  const parsed = plain(core.parseTypedPorts("22, 443 udp:53 161/udp tcp:25 25/tcp 22"));
  assert.deepEqual(parsed, { tcp: [22, 25, 443], udp: [53, 161], invalid: [] });
});
test("typed manual input reports every invalid token and still reads the valid ones", () => {
  const parsed = plain(core.parseTypedPorts("22 udp:99999 foo tcp:80/udp 0 53/udp"));
  assert.deepEqual(parsed.invalid, ["udp:99999", "foo", "tcp:80/udp", "0"]);
  assert.deepEqual([parsed.tcp, parsed.udp], [[22], [53]]);
});
test("the same port number on TCP and UDP stays two ports", () => {
  const parsed = plain(core.parseTypedPorts("53 udp:53"));
  assert.deepEqual([parsed.tcp, parsed.udp], [[53], [53]]);
});
test("merging keeps existing manual ports, adds new ones and never duplicates", () => {
  assert.deepEqual(plain(core.mergePorts([443, 22], [22, 8080, 25])), [22, 25, 443, 8080]);
  assert.equal(core.formatPorts(core.mergePorts([], [])), "");
});
test("only open is confirmed; open|filtered and the other UDP states are uncertain", () => {
  assert.equal(core.describeState("open").confirmed, true);
  for (const state of ["open|filtered", "filtered", "closed|filtered", "unfiltered", "unknown", "", undefined, "weird"]) {
    assert.equal(core.describeState(state).confirmed, false, String(state));
  }
  assert.match(core.describeState("open|filtered").note, /not confirmed/);
  assert.equal(core.describeState("closed").confirmed, false);
});
test("scan rows are distinct, sorted by protocol and port, and malformed entries are skipped", () => {
  const rows = core.collectPorts(result([
    { protocol: "udp", port: 161, state: "open|filtered", service: { name: "snmp", method: "table" } },
    { protocol: "tcp", port: 443, state: "open", service: { name: "https", product: "nginx", version: "1.2", method: "probed" } },
    { protocol: "tcp", port: 443, state: "closed" },
    { protocol: "icmp", port: 1, state: "open" },
    { protocol: "tcp", port: 0, state: "open" },
    { protocol: "tcp", port: "22", state: "open" },
    { protocol: "udp", port: 53, state: "open" }
  ]));
  assert.deepEqual(plain(rows.map(row => `${row.protocol}:${row.port}`)), ["tcp:443", "udp:53", "udp:161"]);
  assert.equal(rows[0].service, "https · nginx · 1.2");
  assert.equal(rows[2].service, "snmp (table hint)");
  assert.equal(rows[1].service, "Unknown");
});
test("service text from a scanner is data only: it is passed through unchanged, never interpreted", () => {
  const rows = core.collectPorts(result([{ protocol: "tcp", port: 80, state: "open", service: { name: "<img src=x onerror=alert(1)>", method: "probed" } }]));
  assert.equal(rows[0].service, "<img src=x onerror=alert(1)>");
});
test("confirmedOpen selects exactly the confirmed rows", () => {
  const rows = core.collectPorts(result([
    { protocol: "tcp", port: 22, state: "open" },
    { protocol: "udp", port: 53, state: "open" },
    { protocol: "udp", port: 161, state: "open|filtered" }
  ]));
  assert.deepEqual(plain(core.confirmedOpen(rows).map(row => `${row.protocol}:${row.port}`)), ["tcp:22", "udp:53"]);
});
test("job states: queued and running show activity, terminal states say what happened", () => {
  assert.equal(core.describeJob(job("queued"), ["tcp"]).kind, "running");
  assert.equal(core.describeJob(job("running"), ["tcp"]).message, "Scanning ports…");
  assert.equal(core.describeJob(job("cancelled"), ["tcp"]).kind, "cancelled");
  assert.equal(core.describeJob(job("expired"), ["tcp"]).kind, "expired");
  assert.equal(core.describeJob(job("weird"), ["tcp"]).tone, "error");
  assert.equal(core.describeJob(null, ["tcp"]).kind, "failed");
});
test("each error code has a sensible message and manual entry stays offered", () => {
  for (const code of ["unavailable", "busy", "timeout", "permission_denied", "invalid_host", "process_failed", "output_limit", "invalid_xml", "forbidden"]) {
    const outcome = core.describeJob(job("failed", { error: { code, message: "server text" } }), ["tcp"]);
    assert.equal(outcome.kind, "failed");
    assert.equal(outcome.tone, "error");
    assert.match(outcome.message, /enter ports manually/i, code);
    assert.equal(outcome.unavailable, code === "unavailable");
  }
  assert.match(core.describeJob(job("failed", { error: { code: "timeout" } }), ["tcp"]).message, /timed out/);
  assert.match(core.describeJob(job("failed", { error: { code: "permission_denied" } }), ["udp"]).message, /permission/);
  assert.match(core.describeJob(job("failed", { error: { code: "other", message: "Custom reason" } }), ["tcp"]).message, /Custom reason/);
});
test("a completed scan without ports is 'no open ports'", () => {
  const outcome = core.describeJob(job("completed", { result: result([]) }), ["tcp"]);
  assert.equal(outcome.kind, "empty");
  assert.match(outcome.message, /No open ports/);
});
test("UDP without a confirmed open port says so and never calls uncertain ports open", () => {
  const rows = [{ protocol: "udp", port: 161, state: "open|filtered" }, { protocol: "tcp", port: 22, state: "open" }];
  const mixed = core.describeJob(job("completed", { result: result(rows) }), ["tcp", "udp"]);
  assert.equal(mixed.kind, "results");
  assert.match(mixed.message, /1 confirmed open/);
  assert.match(mixed.message, /No UDP port could be confirmed open/);
  const only = core.describeJob(job("completed", { result: result([rows[0]]) }), ["udp"]);
  assert.equal(only.kind, "uncertain-only");
  assert.match(only.message, /none confirmed open/);
  const tcpOnly = core.describeJob(job("completed", { result: result([rows[1]]) }), ["tcp"]);
  assert.doesNotMatch(tcpOnly.message, /UDP/);
});
test("request errors map to the same messages and flag a missing worker", () => {
  const unavailable = core.describeRequestError(Object.assign(new Error("x"), { code: "unavailable" }));
  assert.equal(unavailable.unavailable, true);
  assert.equal(unavailable.reason, "x");
  assert.match(core.describeRequestError(new Error("Scan request failed (500).")).message, /500/);
});
test("the scanned protocols follow the active checks, TCP when none is on", () => {
  assert.deepEqual(plain(core.defaultProtocols(false, false)), ["tcp"]);
  assert.deepEqual(plain(core.defaultProtocols(true, true)), ["tcp", "udp"]);
  assert.deepEqual(plain(core.defaultProtocols(false, true)), ["udp"]);
});
const sixteen = () => [
  { protocol: "tcp", port: 80, state: "open", service: { name: "http", method: "table" } },
  { protocol: "tcp", port: 21, state: "closed" },
  { protocol: "tcp", port: 23, state: "closed" },
  ...[111, 135, 139, 445, 1433, 3306, 3389, 5900, 8080, 8443, 9100, 9200].map(port => ({ protocol: "tcp", port, state: "filtered" })),
  { protocol: "udp", port: 161, state: "open|filtered", service: { name: "snmp", method: "table" } },
];
test("16 results with one confirmed open: counts keep closed and unclear apart", () => {
  const rows = core.collectPorts(result(sixteen()));
  assert.deepEqual(plain(core.summarizeRows(rows)), { total: 16, open: 1, closed: 2, unclear: 13 });
  const split = core.splitResults(rows);
  assert.deepEqual(plain(split.confirmed.map(row => `${row.protocol}:${row.port}`)), ["tcp:80"]);
  assert.equal(split.other.length, 15);
  assert.equal(split.other.some(row => row.info.confirmed), false);
  const outcome = core.describeJob(job("completed", { result: result(sixteen()) }), ["tcp", "udp"]);
  assert.equal(outcome.kind, "results");
  assert.match(outcome.message, /16 ports found: 1 confirmed open, 2 closed, 13 unclear\./);
});
test("closed is confirmed closed and never described as uncertain", () => {
  const closed = core.describeState("closed");
  assert.equal(closed.tone, "closed");
  assert.doesNotMatch(closed.note, /uncertain/);
  for (const state of ["filtered", "open|filtered", "closed|filtered", "unfiltered", "unknown"]) {
    assert.equal(core.describeState(state).tone, "uncertain", state);
  }
});
test("zero confirmed open ports is stated factually and never claims every port is closed", () => {
  const outcome = core.describeJob(job("completed", { result: result([{ protocol: "tcp", port: 22, state: "filtered" }, { protocol: "tcp", port: 23, state: "closed" }]) }), ["tcp"]);
  assert.equal(outcome.kind, "uncertain-only");
  assert.match(outcome.message, /2 ports found, none confirmed open \(1 closed, 1 unclear\)\./);
  assert.doesNotMatch(outcome.message, /all .*closed/i);
});
test("only a port-table guess is flagged as a hint; a probed service is not", () => {
  const rows = core.collectPorts(result([
    { protocol: "tcp", port: 80, state: "open", service: { name: "http", method: "table" } },
    { protocol: "tcp", port: 443, state: "open", service: { name: "https", method: "probed" } },
    { protocol: "tcp", port: 25, state: "open" },
  ]));
  const byPort = Object.fromEntries(rows.map(row => [row.port, row]));
  assert.equal(byPort[80].hint, true);
  assert.equal(byPort[80].service, "http (table hint)");
  assert.equal(byPort[443].hint, false);
  assert.equal(byPort[25].hint, false);
});
