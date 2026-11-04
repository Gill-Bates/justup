//
// tests/js/network-scan-picker.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../app/static/js/network-scan-picker.js', import.meta.url), 'utf8');

function el(extra = {}) {
  const listeners = {};
  return {
    value: '', checked: true, hidden: false, disabled: false, textContent: '', dataset: {}, id: '', type: 'text',
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
    fire: (name) => Promise.all((listeners[name] || []).map((fn) => fn())),
    setAttribute() {}, removeAttribute() {}, replaceChildren() {}, dispatchEvent() {},
    ...extra,
  };
}

// Starts a scan for the given host field value and returns the host sent to the API.
async function scannedHost(hostValue) {
  const posted = [];
  const host = el({ value: hostValue });
  const parts = {};
  const root = el({
    dataset: { hostId: 'h', tcpId: 't', udpId: 'u', tcpToggleId: 'tt', udpToggleId: 'ut' },
    closest: () => ({ querySelector: () => el() }),
    querySelector: (sel) => {
      if (sel === '[data-scan-profile]') return el({ value: 'quick' });
      return (parts[sel] ||= el());
    },
  });
  const ids = { h: host, t: el(), u: el(), tt: el(), ut: el() };
  const sandbox = {
    window: {
      apiFetch: async (url, options = {}) => {
        if (options.method === 'POST') {
          posted.push(JSON.parse(options.body).host);
          return { ok: false, status: 500, json: async () => ({ detail: 'stop' }) };
        }
        return { ok: true, status: 200, json: async () => ({ available: true }) };
      },
      setTimeout, addEventListener() {},
    },
    document: {
      getElementById: (id) => ids[id],
      querySelectorAll: () => [root],
      addEventListener() {},
      visibilityState: 'visible',
    },
    AbortController, Set, Number, JSON, Array, URL, Date, Error, Promise, clearTimeout, Event: class {},
  };
  vm.runInNewContext(source, sandbox);
  await new Promise((resolve) => setImmediate(resolve));
  await parts['[data-scan-action="start"]'].fire('click');
  return posted[0];
}

test('scan host: IPv4 URL yields the bare address', async () => {
  assert.equal(await scannedHost('http://192.0.2.10/path'), '192.0.2.10');
});
test('scan host: IPv6 URL loses the URL brackets', async () => {
  assert.equal(await scannedHost('https://[2001:db8::1]:8443/'), '2001:db8::1');
});
test('scan host: bare IPv6 address stays unchanged', async () => {
  assert.equal(await scannedHost('2001:db8::1'), '2001:db8::1');
});
test('scan host: hostname URL yields the hostname', async () => {
  assert.equal(await scannedHost('https://example.org/x'), 'example.org');
});
test('scan host: unparsable URL is passed through', async () => {
  assert.equal(await scannedHost('http://'), 'http://');
});
