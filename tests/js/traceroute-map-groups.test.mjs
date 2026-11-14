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
const aggregateRegions = sandbox.window.TracerouteMap.aggregateOverlapRegions;
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

test('a route that revisits a location keeps separate markers in an ordered lane', () => {
  const points = [{ hop: 6, x: 0 }, { hop: 7, x: 80 }, { hop: 10, x: 2 }];
  const groups = groupNearby(points, (point) => ({ x: point.x, y: 0 }), 36);
  assert.deepEqual(plain(groups.map((group) => group.members.map((member) => member.point.hop))), [[6], [7], [10]]);

  const fanned = sandbox.window.TracerouteMap.fanOutOverlappingHopGroups(groups, 36);
  const revisitDistance = Math.hypot(
    fanned[0].displayCenter.x - fanned[2].displayCenter.x,
    fanned[0].displayCenter.y - fanned[2].displayCenter.y,
  );
  assert.ok(revisitDistance >= 40, `revisited markers should be separated; got ${revisitDistance}px`);
  assert.notEqual(fanned[0].displayCenter.x, fanned[2].displayCenter.x);
  assert.equal(fanned[0].displayCenter.y, fanned[2].displayCenter.y, 'two visits share an ordered row, not radial spokes');
  assert.equal(fanned[1].displayCenter.x, fanned[1].center.x, 'a non-overlapping marker stays at its geographic anchor');
});

test('dense regional overview keeps non-consecutive visits and the distant station', () => {
	const points = [
		{ hop: 1, x: 0, city: 'Frankfurt', country: 'DE' },
		{ hop: 2, x: 1000, city: 'New York', country: 'US' },
		{ hop: 3, x: 2, city: 'Darmstadt', country: 'DE' },
		{ hop: 4, x: 1, city: 'Frankfurt', country: 'DE' },
	];
	const groups = groupNearby(points, (point) => ({ x: point.x, y: 0 }), 36);
	const fanned = sandbox.window.TracerouteMap.fanOutOverlappingHopGroups(groups, 36);
	const overview = aggregateRegions(fanned);
	assert.equal(overview.length, 2, 'Frankfurt-region visits share one marker and New York stays separate');
	assert.deepEqual(plain(overview[0].members.map((member) => member.point.hop)), [1, 3, 4]);
	assert.equal(overview[0].isRegionAggregate, true);
	assert.equal(overview[0].regionLabel, 'DE route area');
	assert.deepEqual(plain(overview[1].members.map((member) => member.point.hop)), [2]);
});

const arrowSegments = sandbox.window.TracerouteMap.computeArrowSegments;

test('each visible transition gets a direction arrow, including dashed and short links', () => {
  // 5 points, four visible segments; segment 1 is estimated and segment 2 is short.
  const lengths = [200, 30, 12, 200];
  const arrows = arrowSegments(5, null, (i) => lengths[i]);
  assert.deepEqual(plain(arrows), [0, 1, 2, 3]);
});

test('segments without a visible path do not get arrows', () => {
  const lengths = [200, 0, 30];
  const arrows = arrowSegments(4, null, (i) => lengths[i]);
  assert.deepEqual(plain(arrows), [0, 2]);
});

test('a selected dashed segment keeps its hop-order arrow', () => {
  assert.deepEqual(plain(arrowSegments(4, 1)), [1]);
});

test('selection shows only the selected visible segment', () => {
  assert.deepEqual(plain(arrowSegments(6, 3)), [3]);
  assert.deepEqual(plain(arrowSegments(3, 9)), []);
  assert.deepEqual(plain(arrowSegments(1, null)), []);
});

test('arrow placement covers each adjacency; line certainty is a separate contract', () => {
  const arrows = arrowSegments(5, null);
  assert.deepEqual(plain(arrows), [0, 1, 2, 3]);
});
const describe = sandbox.window.TracerouteMap.describeMarkerGroup;
const groupOf = (...points) => ({ members: points.map((point, index) => ({ point, index })) });
test('the last mapped hop is a plain hop marker unless the server flagged it as the destination', () => {
  const last = { hop: 11, lat: 51.2, lon: 7.2, city: 'Wuppertal' };
  const info = describe(groupOf(last), 1, false);
  assert.equal(info.isDestination, false);
	assert.equal(info.visibleLabel, 'Wuppertal');
  assert.equal(info.kind, 'first-mapped');
  const dest = describe(groupOf({ ...last, hop: 13, _destination: true }), 1, false);
  assert.equal(dest.isDestination, true);
	assert.equal(dest.visibleLabel, 'Wuppertal · D');
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
