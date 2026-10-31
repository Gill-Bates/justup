//
// tests/js/traceroute-map-groups.test.mjs
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
  document: {
    readyState: 'loading',
    body: { addEventListener() {}, removeEventListener() {} },
    addEventListener() {},
    createElement: element,
  },
  console,
  setTimeout,
  clearTimeout,
};
vm.runInNewContext(source, sandbox);
const groupNearby = sandbox.window.TracerouteMap.groupNearbyHopPoints;
const plain = (value) => JSON.parse(JSON.stringify(value));

test('map marker groups combine hops close enough to overlap at the current zoom', () => {
  const points = [
    { hop: 10, x: 0, y: 0 },
    { hop: 11, x: 22, y: 0 },
    { hop: 12, x: 80, y: 0 },
    { hop: 13, x: 2, y: 1, _interpolated: true },
  ];
  const groups = groupNearby(points, (point) => ({ x: point.x, y: point.y }), 36);
  assert.deepEqual(plain(groups.map((group) => group.members.map((member) => member.point.hop))), [[10, 11], [12]]);
  assert.deepEqual(plain(groups[0].center), { x: 11, y: 0 });
});

test('higher zoom separates nearby hops when their screen positions no longer collide', () => {
  const points = [{ hop: 10, x: 0, y: 0 }, { hop: 11, x: 22, y: 0 }];
  const groups = groupNearby(points, (point) => ({ x: point.x * 2, y: point.y * 2 }), 36);
  assert.equal(groups.length, 2);
});

test('cluster membership is bounded by the full marker radius, not a transitive chain', () => {
  const points = [{ hop: 1, x: 0 }, { hop: 2, x: 25 }, { hop: 3, x: 50 }];
  const groups = groupNearby(points, (point) => ({ x: point.x, y: 0 }), 30);
  assert.deepEqual(plain(groups.map((group) => group.members.map((member) => member.point.hop))), [[1, 2], [3]]);
});

test('a route that revisits a location keeps separate hop markers and fans them out', () => {
  const points = [{ hop: 6, x: 0 }, { hop: 7, x: 80 }, { hop: 10, x: 2 }];
  const groups = groupNearby(points, (point) => ({ x: point.x, y: 0 }), 36);
  assert.deepEqual(plain(groups.map((group) => group.members.map((member) => member.point.hop))), [[6], [7], [10]]);

  const fanned = sandbox.window.TracerouteMap.fanOutOverlappingHopGroups(groups, 36);
  const revisitDistance = Math.hypot(
    fanned[0].displayCenter.x - fanned[2].displayCenter.x,
    fanned[0].displayCenter.y - fanned[2].displayCenter.y,
  );
  assert.ok(revisitDistance >= 40, `revisited markers should be separated; got ${revisitDistance}px`);
  assert.equal(fanned[1].displayCenter.x, fanned[1].center.x);
});


const arrowSegments = sandbox.window.TracerouteMap.computeArrowSegments;

test('direction arrows collapse a contiguous solid run to a single mid-run arrow', () => {
  // 5 points, 4 solid segments -> one arrow in the middle of the run, not four.
  const arrows = arrowSegments(5, () => false, null);
  assert.equal(arrows.length, 1);
  assert.ok(arrows[0] >= 1 && arrows[0] <= 2, `arrow should sit mid-run; got ${arrows[0]}`);
});

test('a dashed gap splits the route into separately-arrowed runs', () => {
  // segments: 0 solid, 1 dashed (gap), 2 solid  -> one arrow per solid run + the gap.
  const dashedAt = (i) => i === 1;
  const arrows = arrowSegments(4, dashedAt, null);
  assert.ok(arrows.includes(1), 'the dashed gap segment is marked');
  assert.ok(arrows.length >= 2, `each run keeps its own arrow; got ${arrows}`);
});

test('a selected segment shows only that one arrow', () => {
  assert.deepEqual(plain(arrowSegments(6, () => false, 3)), [3]);
});

test('an out-of-range selection shows no arrow', () => {
  assert.deepEqual(plain(arrowSegments(3, () => false, 9)), []);
});

test('a single point yields no arrows', () => {
  assert.deepEqual(plain(arrowSegments(1, () => false, null)), []);
});
