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

test('a dashed gap splits the route into separately-arrowed solid runs and never carries an arrow itself', () => {
  // segments: 0 solid, 1 dashed (gap), 2 solid, 3 solid -> one arrow per solid run, none on the gap.
  const dashedAt = (i) => i === 1;
  const arrows = arrowSegments(5, dashedAt, null);
  assert.ok(!arrows.includes(1), 'the dashed gap segment carries no arrow');
  assert.deepEqual(plain(arrows), [0, 2]);
});

test('segments shorter than the minimum length carry no arrow', () => {
  const lengths = [200, 30, 200];
  assert.deepEqual(plain(arrowSegments(4, () => false, null, (i) => lengths[i], 64)), [0, 2]);
  assert.deepEqual(plain(arrowSegments(4, () => false, 1, (i) => lengths[i], 64)), []);
  assert.deepEqual(plain(arrowSegments(4, () => false, 2, (i) => lengths[i], 64)), [2]);
});

test('a selected dashed segment shows no arrow', () => {
  assert.deepEqual(plain(arrowSegments(4, (i) => i === 1, 1)), []);
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
const describe = sandbox.window.TracerouteMap.describeMarkerGroup;
const groupOf = (...points) => ({ members: points.map((point, index) => ({ point, index })) });
test('the last mapped hop is a plain hop marker unless the server flagged it as the destination', () => {
  const last = { hop: 11, lat: 51.2, lon: 7.2, city: 'Wuppertal' };
  const info = describe(groupOf(last), 1, false);
  assert.equal(info.isDestination, false);
  assert.equal(info.visibleLabel, '11');
  assert.equal(info.kind, 'first-mapped');
  const dest = describe(groupOf({ ...last, hop: 13, _destination: true }), 1, false);
  assert.equal(dest.isDestination, true);
  assert.equal(dest.visibleLabel, 'D');
  assert.equal(dest.kind, 'destination');
});
test('a destination sharing a marker with earlier hops lists them next to D', () => {
  const info = describe(groupOf({ hop: 12 }, { hop: 13, _destination: true }), 1, false);
  assert.equal(info.visibleLabel, '12·D');
});
test('hops merged across an unmapped hop list every included hop instead of a range', () => {
  const info = describe(groupOf({ hop: 7 }, { hop: 10 }), 1, false);
  assert.equal(info.visibleLabel, '7·10');
  assert.equal(sandbox.window.TracerouteMap.pointLabel({ hop: 3, _hop_range: '3-5' }), '3–5');
});
test('start and destination markers keep their S and D labels', () => {
  assert.equal(describe(groupOf({ hop: 1 }, { hop: 2 }), 1, true).visibleLabel, 'S·2');
  assert.equal(describe(groupOf({ hop: 1, _destination: true }), 1, true).visibleLabel, 'S/D');
});
test('approximate map positions stay distinguishable in marker groups', () => {
  assert.equal(describe(groupOf({ hop: 3, _approximate: true }), 1, false).isApproximate, true);
});
// Chain as the server emits it for Frankfurt -> Kottenheim (approx.) -> Frankfurt -> Wuppertal -> Ratingen (approx. destination).
// tests/test_pdf_route_map.py pins the same segment sequence for the PDF reader.
const owner = [
  { hop: 7, map_role: 'route', segment_to_next: 'dashed' },
  { hop: 8, map_role: 'estimate', _approximate: true, segment_to_next: 'dashed' },
  { hop: 10, map_role: 'route', segment_to_next: 'solid' },
  { hop: 11, map_role: 'route', segment_to_next: 'dashed' },
  { hop: 13, map_role: 'route', _destination: true, _approximate: true },
];
test('an estimate is a node of the chain and reliable back-jumps stay', () => {
  const route = sandbox.window.TracerouteMap.routePointsOf(owner);
  assert.deepEqual(plain(route.map((p) => p.hop)), [7, 8, 10, 11, 13]);
  const dashed = sandbox.window.TracerouteMap.isDashedSegment;
  assert.deepEqual(plain(route.slice(0, -1).map((p) => dashed(p))), [true, true, false, true]);
  // A reliable A-B-A sequence is untouched: every point keeps the route role.
  const aba = [{ hop: 1, segment_to_next: 'solid' }, { hop: 2, segment_to_next: 'solid' }, { hop: 3 }];
  assert.equal(sandbox.window.TracerouteMap.routePointsOf(aba).length, 3);
});
test('an estimate keeps its own tilde marker and never shares one with a route hop', () => {
  const groups = groupNearby(owner.map((p, i) => ({ ...p, x: p.hop === 13 ? 200 : i * 2 })), (point) => ({ x: point.x, y: 0 }), 36);
  assert.deepEqual(plain(groups.map((group) => group.members.map((member) => member.point.hop))), [[7], [8], [10, 11], [13]]);
  const info = describe(groups[1], 1, false);
  assert.deepEqual(plain([info.kind, info.visibleLabel, info.isEstimate, info.isApproximate]), ['estimate', '~8', true, true]);
  const dest = describe(groups[3], 1, false);
  assert.deepEqual(plain([dest.kind, dest.visibleLabel, dest.isApproximate]), ['destination', 'D', true]);
});
