//
// tests/js/signal-status-refresh.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from 'node:assert/strict';
import test from 'node:test';

function element() {
  return {
    innerHTML: '', textContent: '', hidden: false, style: {}, children: [],
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    replaceChildren(...children) { this.children = children; this.innerHTML = ''; },
    appendChild(child) { this.children.push(child); },
    append(...children) { this.children.push(...children); },
    remove() {},
  };
}

const elements = new Map();
globalThis.document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  },
  createElement: element,
  createTextNode: text => ({ textContent: text }),
  addEventListener() {},
};
const document = globalThis.document;
globalThis.window = globalThis;
const window = globalThis.window;
window.JuStatusDot = { html: (_tone, label) => `<span>${label}</span>` };
let respond;
const urls = [];
window.apiFetch = async url => {
  urls.push(url);
  return respond();
};

const { loadSignalStatus } = await import('../../app/static/js/settings.signal-status.js');
const unavailable = () => ({ ok: true, json: async () => ({ reachable: false, registered: false, error: 'offline' }) });

test('refresh keeps the last known status visible and bypasses backend cache', async () => {
  const badges = document.getElementById('signal-status-badges');
  const mode = document.getElementById('signal-mode-selection');
  badges.innerHTML = '<span>Linked</span>';
  mode.hidden = false;
  let finish;
  respond = () => new Promise(resolve => { finish = resolve; });

  const refresh = loadSignalStatus({ forceRefresh: true });
  assert.equal(urls.at(-1), '/settings/signal/status?refresh=true');
  assert.equal(badges.innerHTML, '<span>Linked</span>');
  assert.equal(mode.hidden, false);
  finish(unavailable());
  await refresh;
  assert.equal(badges.innerHTML, '<span>Offline</span>');
});

test('forced refresh arriving during a status fetch runs after it', async () => {
  const requests = [];
  respond = () => new Promise(resolve => { requests.push(resolve); });
  const first = loadSignalStatus();
  await loadSignalStatus({ forceRefresh: true });
  assert.equal(requests.length, 1);
  requests[0](unavailable());
  for (let i = 0; i < 5 && requests.length < 2; i++) await Promise.resolve();
  assert.equal(requests.length, 2);
  assert.equal(urls.at(-1), '/settings/signal/status?refresh=true');
  requests[1](unavailable());
  await first;
});
