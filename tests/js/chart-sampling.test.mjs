//
// tests/js/chart-sampling.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
const source = readFileSync(new URL('../../app/static/js/charts/chart-sampling.js', import.meta.url), 'utf8');
function setup() {
  const winListeners = new Set();
  const bodyListeners = {};
  const body = { addEventListener: (type, fn) => { bodyListeners[type] = fn; } };
  const window = {
    addEventListener: (type, fn) => { if (type === 'resize') winListeners.add(fn); },
    removeEventListener: (type, fn) => { if (type === 'resize') winListeners.delete(fn); },
  };
  vm.runInNewContext(source, { window, document: { body, documentElement: {} }, setTimeout, clearTimeout });
  return { window, body, winListeners, swap: bodyListeners['htmx:beforeSwap'] };
}
test('a fragment swap keeps the resize listener and the global hooks', () => {
  const { window, body, winListeners, swap } = setup();
  window._chartSamplingInit();
  swap({ detail: { target: { id: 'downtime-history' } } });
  assert.equal(winListeners.size, 1);
  assert.equal(typeof window._chartSamplingInit, 'function');
  assert.equal(typeof window.updateChartSampling, 'function');
  swap({ detail: { target: body } });
  assert.equal(winListeners.size, 0);
  assert.equal(window._chartSamplingInit, undefined);
});
