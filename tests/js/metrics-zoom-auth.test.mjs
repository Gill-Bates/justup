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
