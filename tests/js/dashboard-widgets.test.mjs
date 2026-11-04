//
// tests/js/dashboard-widgets.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const window = {};
vm.runInNewContext(readFileSync(new URL('../../app/static/js/dashboard-widgets.js', import.meta.url), 'utf8'), { window });
vm.runInNewContext(readFileSync(new URL('../../app/static/js/dashboard-monitoring.js', import.meta.url), 'utf8'), {
  window, document: { getElementById: () => null },
});
const { queueState } = window.JuDashboardMonitoring;
const now = Date.parse('2026-11-01T12:00:00Z');
const clear = { pending: 0, processing: 0, failed: 0, stuck_processing: 0, processing_timeout_seconds: 300, oldest_pending: null, next_retry_at: null, services: { monitor_scheduler: { state: 'running' } } };

test('factory layouts retain six originals and expose only allowed defaults', () => {
  const normal = window.JuDashboardRegistry.forRole(false);
  const admin = window.JuDashboardRegistry.forRole(true);
  assert.equal(normal.filter(entry => entry.defaultVisible).length, 7);
  assert.equal(admin.filter(entry => entry.defaultVisible).length, 8);
  assert.ok(!normal.some(entry => entry.id === 'delivery-queue'));
  assert.equal(normal.find(entry => entry.id === 'quality').defaultSize.h, 3);
  assert.equal(admin.find(entry => entry.id === 'delivery-queue').defaultSize.h, 3);
  assert.equal(admin.find(entry => entry.id === 'delivery-queue').minH, 3);
  const alerts = admin.find(entry => entry.id === 'alerts-open');
  assert.equal(alerts.label, 'Alerts');
  const queue = admin.find(entry => entry.id === 'delivery-queue');
  assert.equal(alerts.defaultX, queue.defaultX);
  assert.equal(alerts.defaultY, queue.defaultY + queue.defaultSize.h);
});

test('queue distinguishes backlog from failed history and unavailable data', () => {
  assert.equal(queueState(clear, now)[0], 'Queue clear');
  assert.equal(queueState({ ...clear, failed: 3 }, now)[0], 'Handoff failed');
  assert.equal(queueState({ ...clear, failed: 3, failed_recent: 3 }, now).join(), 'Handoff failed,danger');
  // Older failures outside the 24 h window no longer turn an empty queue red.
  assert.equal(queueState({ ...clear, failed: 3, failed_recent: 0 }, now)[0], 'Queue clear');
  const pending = { ...clear, pending: 1, oldest_pending: new Date(now - 60000).toISOString() };
  assert.equal(queueState(pending, now)[0], 'Processing');
  assert.equal(queueState({ ...pending, oldest_pending: new Date(now - 300000).toISOString() }, now)[0], 'Backlog');
  assert.equal(queueState({ ...pending, oldest_pending: new Date(now - 1800000).toISOString() }, now)[0], 'Stalled');
  assert.equal(queueState({ ...clear, stuck_processing: 1 }, now)[0], 'Stalled');
  assert.equal(queueState({ ...clear, services: { monitor_scheduler: { state: 'degraded' } } }, now)[0], 'Worker degraded');
  assert.equal(queueState({ ...pending, oldest_pending: 'bad' }, now)[0], 'Status unavailable');
  assert.equal(queueState({ ...clear, pending: undefined }, now)[0], 'Status unavailable');
  assert.equal(queueState({ ...clear, services: {} }, now)[0], 'Status unavailable');
  assert.equal(queueState({ ...clear, stuck_processing: 1, services: { monitor_scheduler: { state: 'degraded' } } }, now)[0], 'Stalled');
});
