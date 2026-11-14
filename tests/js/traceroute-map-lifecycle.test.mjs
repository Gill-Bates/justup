//
// tests/js/traceroute-map-lifecycle.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
const source = readFileSync(new URL('../../app/static/js/traceroute-map.js', import.meta.url), 'utf8');
function setup({ geoPoints = 'not-json', leaflet = {} } = {}) {
  const errors = [];
  const bodyListeners = [];
  const container = {
    isConnected: true,
    dataset: { geoPoints, mapId: 'm1' },
    style: {},
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    removeAttribute(k) { delete this.attrs[k]; },
    matches() { return false; },
    appendChild(child) { this.children ||= []; this.children.push(child); },
    addEventListener() {},
    getBoundingClientRect() { return { left: 0, top: 0, right: 400, bottom: 200, width: 400, height: 200 }; },
    closest() { return null; },
  };
  const root = { querySelectorAll: (selector) => (selector.includes('traceroute-map-container') ? [container] : []), matches: () => false };
  const removed = { stop: 0, off: 0, remove: 0, markers: 0, polylines: 0, fitBounds: 0, setView: 0 };
  const routeLines = [];
  const map = {
    stop() { removed.stop += 1; }, off() { removed.off += 1; }, remove() { removed.remove += 1; },
    createPane() {}, getPane() { return { style: {} }; },
    // 20 px per degree: far enough apart that the markers do not touch and a link remains.
    project([lat, lon]) { return { x: 200 + (lon - 10) * 20, y: 100 + (lat - 51) * 20 }; },
    unproject(point) {
      const [x, y] = Array.isArray(point) ? point : [point.x, point.y];
      return [y / 20, x / 20];
    },
    getZoom() { return 4; }, getSize() { return { x: 400, y: 200 }; },
    getBounds() { return { getEast() { return 180; }, getWest() { return -180; } }; },
    setMaxBounds() {}, setZoom() {},
    getPixelOrigin() { return { x: 1128, y: 916 }; }, getContainer() { return { getBoundingClientRect() { return { left: 0, top: 0 }; } }; },
    containerPointToLayerPoint([x, y]) { return { x, y }; }, latLngToContainerPoint() { return { x: 100, y: 100 }; },
    invalidateSize() {}, panBy() {}, fitBounds() { removed.fitBounds += 1; }, setView() { removed.setView += 1; },
    on() {},
  };
  const layerGroup = () => ({ addTo() { return this; }, clearLayers() {}, addLayer() {} });
  const marker = (_latlng, options = {}) => {
    removed.markers += 1;
    const iconElement = options.icon?.options?.html;
    return { addTo() { return this; }, bindPopup() { return this; }, bindTooltip() { return this; }, on() {}, setLatLng() { return this; }, getElement() { return iconElement || null; } };
  };
  const polyline = (points) => {
    removed.polylines += 1;
    routeLines.push(points);
    return { addTo() { return this; }, on() {}, setStyle() {}, bindTooltip() { return this; }, getElement() { return null; } };
  };
  const element = () => ({
    style: {}, dataset: {}, classList: { toggle() {} }, setAttribute() {}, appendChild() {}, addEventListener() {}, remove() {}, getBoundingClientRect() { return { left: 100, top: 100, right: 120, bottom: 120, width: 20, height: 20 }; },
  });
  const sandbox = {
    window: {
      L: {
        ...leaflet,
        map() { return map; }, layerGroup, marker, polyline, divIcon: ({ html }) => ({ options: { html } }),
        geoJSON: () => ({ addTo() { return this; }, setStyle() {} }),
        latLngBounds: () => ({ pad() { return this; } }), circle: () => ({ addTo() { return this; } }),
      },
      addEventListener() {}, removeEventListener() {},
    },
    document: {
      readyState: 'complete',
      body: { addEventListener(type, fn) { bodyListeners.push([type, fn]); }, removeEventListener() {} },
      addEventListener() {},
      createElement: element,
      createTextNode: () => ({}),
      documentElement: { getAttribute: () => 'light' },
      querySelectorAll: () => [],
    },
    console: { error: (...args) => errors.push(args.map((arg) => arg?.message || arg)), warn() {} },
    escapeHtml: (value) => String(value),
    setTimeout,
    clearTimeout,
    fetch: async () => ({ ok: false, status: 503 }),
    requestAnimationFrame: (fn) => fn(),
    ResizeObserver: class { observe() {} disconnect() {} },
  };
  vm.runInNewContext(source, sandbox);
  return { api: sandbox.window.TracerouteMap, container, root, errors, bodyListeners, removed, routeLines };
}
const tick = () => new Promise((resolve) => setImmediate(resolve));
const VALID_POINTS = JSON.stringify([
  { lat: 50, lon: 7, hop: 1, city: 'Cologne', country: 'DE' },
  { lat: 52, lon: 13, hop: 2, city: 'Berlin', country: 'DE' },
]);
test('valid geo points initialise a route and cleanup releases the Leaflet map', async () => {
  const points = VALID_POINTS;
  const { api, root, container, errors, removed, routeLines } = setup({ geoPoints: points });
  api.initAllMaps(root);
  await tick();
  assert.equal(errors.length, 0, JSON.stringify(errors));
  assert.ok(container._leaflet_map, 'the map was created');
  // The visible link is trimmed to stop before each marker, so it lies strictly between the two hops.
  assert.ok(routeLines.some((line) => line.length >= 2), `the route segment was created: ${JSON.stringify(routeLines)}`);
  assert.ok(removed.markers >= 2, 'hop markers and direction marker were created');
  assert.equal(removed.fitBounds, 1, 'the route bounds were fitted');

  api.cleanupAll(root);
  assert.equal(container._leaflet_map, null);
  assert.equal(container._resetControl, null);
  assert.deepEqual({ stop: removed.stop, off: removed.off, remove: removed.remove }, { stop: 1, off: 1, remove: 1 });
});
test('a fragment cleaned up during async Leaflet init is not initialised', async () => {
  const { api, root, container, errors, removed } = setup({ geoPoints: VALID_POINTS });
  api.initAllMaps(root);
  api.cleanupAll(root);
  await tick();
  assert.equal(errors.length, 0);
  assert.ok(!container._leaflet_map, 'no map was created');
  assert.equal(removed.fitBounds, 0);
});
test('a container detached during async Leaflet init is not initialised', async () => {
  const { api, root, container, errors, removed } = setup({ geoPoints: VALID_POINTS });
  api.initAllMaps(root);
  container.isConnected = false;
  await tick();
  assert.equal(errors.length, 0);
  assert.ok(!container._leaflet_map, 'no map was created');
  assert.equal(removed.fitBounds, 0);
});
test('initialising a map keeps its HTMX and pagehide cleanup handlers registered', async () => {
  const { api, root, container, bodyListeners } = setup();
  api.initAllMaps(root);
  await tick();
  assert.equal(typeof container._htmxHandler, 'function');
  assert.equal(typeof container._pagehideHandler, 'function');
  assert.ok(bodyListeners.some(([type, fn]) => type === 'htmx:beforeSwap' && fn === container._htmxHandler));
  container._pagehideHandler();
  assert.equal(container._htmxHandler, null);
  assert.equal(container._pagehideHandler, null);
});
