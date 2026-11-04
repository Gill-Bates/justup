//
// tests/js/traceroute-map-layout.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../app/static/js/traceroute-map.js', import.meta.url), 'utf8');
const element = () => ({ textContent: '', innerHTML: '', className: '', style: {}, setAttribute() {} });
const sandbox = {
  window: { addEventListener() {}, removeEventListener() {} },
  document: { readyState: 'loading', body: { addEventListener() {} }, addEventListener() {}, createElement: element },
  console, setTimeout, clearTimeout,
};
vm.runInNewContext(source, sandbox);
const { placeLabels, trimChainSegment, chainBounds, segmentObstacles } = sandbox.window.TracerouteMap;
const plain = (value) => JSON.parse(JSON.stringify(value));
const RULES = { countryMaxZoom: 5, cityMinZoom: 4 };
// One pixel per 0.1 degree: easy to reason about collisions.
const project = (lat, lon) => ({ x: lon * 10, y: -lat * 10 });
const LABELS = {
  countries: [{ name: 'Germany', lat: 54, lon: 10 }, { name: 'France', lat: 46, lon: 2 }],
  cities: [
    { name: 'Frankfurt', lat: 50.1, lon: 8.7, role: 'path' },
    { name: 'Cologne', lat: 50.9, lon: 6.9, role: 'orientation' },
  ],
};
const names = (placed) => plain(Array.from(placed, (item) => item.name));

test('zoom levels gate the names: countries at overview, places once zoomed in', () => {
  assert.deepEqual(names(placeLabels(LABELS, 3, RULES, project, [])), ['Germany', 'France']);
  assert.deepEqual(names(placeLabels(LABELS, 4, RULES, project, [])), ['Frankfurt', 'Germany', 'France', 'Cologne']);
  assert.deepEqual(names(placeLabels(LABELS, 6, RULES, project, [])), ['Frankfurt', 'Cologne']);
});

test('a name that would overlap a marker or the chain is moved aside or dropped', () => {
  const marker = [{ x1: 70, x2: 110, y1: -520, y2: -480 }]; // on the Frankfurt anchor (87, -501)
  const placed = placeLabels(LABELS, 4, RULES, project, marker);
  const frankfurt = placed.find((item) => item.name === 'Frankfurt');
  assert.ok(frankfurt && frankfurt.side !== 'right', 'moved to another side of the marker');
  assert.ok(!(frankfurt.rect.x1 < 110 && frankfurt.rect.x2 > 70 && frankfurt.rect.y1 < -480 && frankfurt.rect.y2 > -520));
  const blocked = [{ x1: -2000, x2: 2000, y1: -2000, y2: 2000 }];
  assert.deepEqual(names(placeLabels(LABELS, 4, RULES, project, blocked)), []);
});

test('placed names never overlap each other', () => {
  const dense = { countries: [], cities: ['Alpha', 'Alpine', 'Alpaca'].map((name) => ({ name, lat: 50, lon: 8, role: 'orientation' })) };
  const placed = placeLabels(dense, 5, RULES, project, []);
  for (const a of placed) for (const b of placed) {
    if (a !== b) assert.ok(a.rect.x2 <= b.rect.x1 || b.rect.x2 <= a.rect.x1 || a.rect.y2 <= b.rect.y1 || b.rect.y2 <= a.rect.y1);
  }
});

test('the chain link stops a gap before each marker and vanishes when markers touch', () => {
  const size = { w: 32, h: 32 };
  const seg = trimChainSegment({ x: 0, y: 0 }, { x: 100, y: 0 }, size, size, 3);
  assert.deepEqual(plain({ a: seg.a, b: seg.b, length: seg.length }), { a: { x: 19, y: 0 }, b: { x: 81, y: 0 }, length: 62 });
  assert.equal(trimChainSegment({ x: 0, y: 0 }, { x: 36, y: 0 }, size, size, 3), null);
  assert.equal(trimChainSegment({ x: 5, y: 5 }, { x: 5, y: 5 }, size, size, 3), null);
  const wide = trimChainSegment({ x: 0, y: 0 }, { x: 200, y: 0 }, { w: 80, h: 32 }, size, 3);
  assert.equal(wide.a.x, 43, 'a wider marker keeps the line further away');
});

test('the fit bounds contain every chain node, an outlier included', () => {
  const bounds = chainBounds([{ lat: 50, lon: 8 }, { lat: 32, lon: 34 }, { lat: 51, lon: 6 }, { lat: NaN, lon: 1 }]);
  assert.deepEqual(plain(bounds), [[50, 8], [32, 34], [51, 6]]);
});

test('segment obstacles cover the whole link', () => {
  const rects = segmentObstacles({ x: 0, y: 0 }, { x: 60, y: 0 }, 2);
  assert.ok(rects.length >= 5);
  assert.equal(rects[0].x1, -2);
  assert.equal(rects[rects.length - 1].x2, 62);
});
