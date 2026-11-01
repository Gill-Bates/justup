//
// tests/js/metrics-zoom-auth.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../app/static/js/target-detail/metrics.js', import.meta.url), 'utf8');

test('zoom-range metric requests use the authenticated apiFetch wrapper and preserve abort signals', async () => {
  const requests = [];
  const signal = { aborted: false };
  const context = {
    apiFetch: async (url, options) => {
      requests.push({ url, options });
      return { ok: true, json: async () => ({ points: [] }) };
    },
    fetch: () => { throw new Error('bare fetch must not be used for authenticated metrics'); },
    URLSearchParams,
    Date,
    encodeURIComponent,
    console,
  };
  vm.runInNewContext(source, context);
  const result = await context.fetchDisplayMetricRange(7, 'http_ms', 0, 60_000, 640, signal);

  assert.deepEqual(requests.map(request => request.url), [
    '/targets/7/metrics/http_ms?display=true&plot_width=640&since=1970-01-01T00%3A00%3A00.000Z&until=1970-01-01T00%3A01%3A00.000Z',
  ]);
  assert.equal(requests[0].options.signal, signal);
  assert.deepEqual(result, { points: [] });
});
function abortAwareContext(overrides = {}) {
  const warnings = [];
  const token = Symbol('load');
  const context = {
    appState: {
      range: { from: new Date(0), to: new Date(60_000) },
      metricsCache: new Map(),
      currentLoadToken: token,
    },
    document: { getElementById: () => null },
    window: { innerWidth: 900 },
    apiFetch: (url, options) => new Promise((resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
    }),
    URLSearchParams, Date, encodeURIComponent,
    console: { warn: () => warnings.push('warn'), error: () => warnings.push('error') },
    ...overrides,
  };
  vm.runInNewContext(source, context);
  return { context, token, warnings };
}
test('aborting a load cycle cancels in-flight metric requests without warnings or toasts', async () => {
  const { context, token, warnings } = abortAwareContext();
  const controller = new AbortController();
  const metric = context.fetchMetricGuarded(1, 'http_ms', token, controller.signal);
  const series = context.fetchAvailabilitySeries(1, token, controller.signal);
  controller.abort();
  assert.equal(await metric, null);
  assert.equal(await series, null);
  assert.deepEqual(warnings, []);
});
test('fetchMetric rethrows AbortError so callers can tell cancellation from failure', async () => {
  const { context } = abortAwareContext();
  const controller = new AbortController();
  const pending = context.fetchMetric(1, 'http_ms', false, controller.signal);
  controller.abort();
  await assert.rejects(pending, { name: 'AbortError' });
});
test('non-abort network errors still surface as an error result and is logged', async () => {
  const { context, warnings } = abortAwareContext({ apiFetch: async () => { throw new Error('boom'); } });
  const result = await context.fetchMetric(1, 'http_ms', false, new AbortController().signal);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), { points: [], error: true });
  assert.ok(warnings.includes('error'));
});
