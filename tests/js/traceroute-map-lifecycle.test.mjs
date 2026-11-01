//
// tests/js/traceroute-map-lifecycle.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
const source = readFileSync(new URL('../../app/static/js/traceroute-map.js', import.meta.url), 'utf8');
function setup() {
  const errors = [];
  const bodyListeners = [];
  const container = {
    isConnected: true,
    dataset: { geoPoints: 'not-json', mapId: 'm1' },
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    removeAttribute(k) { delete this.attrs[k]; },
    matches() { return false; },
  };
  const root = { querySelectorAll: (selector) => (selector.includes('traceroute-map-container') ? [container] : []), matches: () => false };
  const sandbox = {
    window: { L: {}, addEventListener() {}, removeEventListener() {} },
    document: {
      readyState: 'complete',
      body: { addEventListener(type, fn) { bodyListeners.push([type, fn]); }, removeEventListener() {} },
      addEventListener() {},
      createElement: () => ({}),
      querySelectorAll: () => [],
    },
    console: { error: (...args) => errors.push(args) },
    setTimeout,
    clearTimeout,
  };
  vm.runInNewContext(source, sandbox);
  return { map: sandbox.window.TracerouteMap, container, root, errors, bodyListeners };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));
test('a map is initialised once Leaflet is ready', async () => {
  const { map, root, errors } = setup();
  map.initAllMaps(root);
  await tick();
  assert.equal(errors.length, 1, 'initMap ran (invalid JSON is its observable first step)');
});
test('a fragment cleaned up during async Leaflet init is not initialised', async () => {
  const { map, root, errors } = setup();
  map.initAllMaps(root);
  map.cleanupAll(root);
  await tick();
  assert.equal(errors.length, 0);
});
test('a container detached during async Leaflet init is not initialised', async () => {
  const { map, root, container, errors } = setup();
  map.initAllMaps(root);
  container.isConnected = false;
  await tick();
  assert.equal(errors.length, 0);
});
test('initialising a map keeps its HTMX and pagehide cleanup handlers registered', async () => {
  const { map, root, container, bodyListeners } = setup();
  map.initAllMaps(root);
  await tick();
  assert.equal(typeof container._htmxHandler, 'function');
  assert.equal(typeof container._pagehideHandler, 'function');
  assert.ok(bodyListeners.some(([type, fn]) => type === 'htmx:beforeSwap' && fn === container._htmxHandler));
  container._pagehideHandler();
  assert.equal(container._htmxHandler, null);
  assert.equal(container._pagehideHandler, null);
});
