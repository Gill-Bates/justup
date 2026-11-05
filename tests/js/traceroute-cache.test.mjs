//
// tests/js/traceroute-cache.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../app/static/js/target-detail/traceroute-cache.js', import.meta.url), 'utf8');
const T0 = Date.UTC(2026, 9, 31, 22, 15);
const TTL = 86_400_000;
const URL_ = '/ui/fragments/traceroute/7';

/** Storage double; `quota` caps the total characters, `broken` makes every write throw. */
class FakeStorage {
  constructor({ quota = Infinity, broken = false } = {}) { this.map = new Map(); this.quota = quota; this.broken = broken; }
  get length() { return this.map.size; }
  key(i) { return [...this.map.keys()][i] ?? null; }
  getItem(k) { return this.map.has(k) ? this.map.get(k) : null; }
  removeItem(k) { this.map.delete(k); }
  setItem(k, v) {
    const size = [...this.map].filter(([key]) => key !== k).reduce((n, [, val]) => n + val.length, 0) + String(v).length;
    if (this.broken || size > this.quota) throw new Error('QuotaExceededError');
    this.map.set(k, String(v));
  }
}

function loadModule(win = {}) {
  const ctx = vm.createContext({ console, setTimeout, clearTimeout, ...win });
  vm.runInContext(source, ctx);
  return ctx.TracerouteCache;
}
const Cache = loadModule();

function reply({ status = 200, html = '<p>route</p>', measuredAt, headers = {} } = {}) {
  const h = { ...headers };
  if (measuredAt !== undefined) h['x-measured-at'] = String(measuredAt);
  return { status, ok: status >= 200 && status < 300, headers: { get: (n) => h[n.toLowerCase()] ?? null }, text: async () => html };
}

/** One page: loader plus recorders. `responses` is consumed in order by the fake fetch. */
function page({ storage, clock, user = 'u1', target = 't1', geo, hidden = false, active = true, locks = null, responses = [] } = {}) {
  const rec = { calls: [], renders: [], statuses: [], busy: [], loadErrors: [], redirects: [], timers: [], loading: 0 };
  const state = { hidden, active };
  const store = Cache.createStore({ storage, now: () => clock.t });
  const loader = Cache.createLoader({
    url: URL_, scope: geo === undefined ? { user, target, build: 'b1' } : { user, target, build: 'b1', geo }, store,
    now: () => clock.t,
    fetchFn: async (url) => {
      rec.calls.push(url);
      const next = responses.shift();
      if (next instanceof Error) throw next;
      return next ?? reply({ status: 500 });
    },
    render: (r) => rec.renders.push(r),
    onRedirect: (u) => rec.redirects.push(u),
    locks, isHidden: () => state.hidden, isActive: () => state.active,
    setBusy: (b) => rec.busy.push(b), setStatus: (s) => rec.statuses.push(s),
    setLoading: () => { rec.loading += 1; }, setLoadError: (m) => rec.loadErrors.push(m),
    setTimer: (fn, ms) => { rec.timers.push({ fn, ms }); return rec.timers.length; }, clearTimer: () => {},
    owner: `${user}-${Math.random()}`,
  });
  return { loader, store, rec, state, clock };
}
const plain = (value) => JSON.parse(JSON.stringify(value)); // detach from the vm realm
const stored = (storage) => [...storage.map.entries()].filter(([k]) => k.includes('.e.')).map(([, v]) => JSON.parse(v));
const seed = (storage, clock, measuredAt, extra = {}) =>
  Cache.createStore({ storage, now: () => clock.t }).write({ user: 'u1', target: 't1', build: 'b1' }, { html: '<p>cached</p>', measuredAt, ...extra });

test('cache miss loads once from the server and stores the measurement time', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  const p = page({ storage, clock, responses: [reply({ measuredAt: T0 - 1000 })] });
  await p.loader.show();
  assert.deepEqual(p.rec.calls, [URL_]);
  assert.equal(p.rec.loading, 1);
  assert.equal(p.rec.renders.length, 1);
  const [entry] = stored(storage);
  assert.equal(entry.measuredAt, T0 - 1000);
  assert.deepEqual(Object.keys(entry).sort(), ['build', 'html', 'measuredAt', 'schemaVersion', 'target', 'user']);
});

test('reload 30 minutes later renders from the cache without any request', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  await page({ storage, clock, responses: [reply({ measuredAt: T0 })] }).loader.show();
  clock.t = T0 + 30 * 60_000;
  const reloaded = page({ storage, clock });
  await reloaded.loader.show();
  assert.equal(reloaded.rec.calls.length, 0);
  assert.equal(reloaded.rec.renders.length, 1);
  assert.deepEqual(plain(reloaded.rec.statuses.at(-1)), { kind: 'scanned', measuredAt: T0 });
  assert.equal(stored(storage)[0].measuredAt, T0, 'a cache hit never moves measuredAt');
});

test('TTL boundary: a hit at 24 h - 1 ms, one automatic refresh=1 scan at exactly 24 h', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  seed(storage, clock, T0);
  clock.t = T0 + TTL - 1;
  const hit = page({ storage, clock });
  await hit.loader.show();
  assert.equal(hit.rec.calls.length, 0);
  clock.t = T0 + TTL;
  const miss = page({ storage, clock, responses: [reply({ html: '<p>new</p>', measuredAt: T0 + TTL })] });
  await miss.loader.show();
  assert.deepEqual(miss.rec.calls, [`${URL_}?refresh=1`]);
  assert.equal(miss.rec.renders.at(-1).html, '<p>new</p>');
  assert.equal(stored(storage)[0].measuredAt, T0 + TTL);
});

test('repeated navigation on a loaded page neither renders again nor requests', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  const p = page({ storage, clock, responses: [reply({ measuredAt: T0 })] });
  await p.loader.show();
  await p.loader.show();
  await p.loader.show();
  assert.equal(p.rec.calls.length, 1);
  assert.equal(p.rec.renders.length, 1, 'one render means one Leaflet init');
});

test('manual refresh before the TTL requests refresh=1 and replaces html and timestamp', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  seed(storage, clock, T0);
  const p = page({ storage, clock, responses: [reply({ html: '<p>fresh</p>', measuredAt: T0 + 60_000 })] });
  await p.loader.show();
  clock.t = T0 + 120_000;
  await p.loader.refresh();
  assert.deepEqual(p.rec.calls, [`${URL_}?refresh=1`]);
  assert.equal(p.rec.renders.at(-1).html, '<p>fresh</p>');
  const [entry] = stored(storage);
  assert.equal(entry.html, '<p>fresh</p>');
  assert.equal(entry.measuredAt, T0 + 60_000);
  assert.deepEqual(p.rec.busy, [true, false]);
});

test('failed manual refresh keeps the previous result and timestamp', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  seed(storage, clock, T0);
  const p = page({ storage, clock, responses: [reply({ status: 500 }), reply({ html: 'No hops' }), new TypeError('offline')] });
  await p.loader.show();
  clock.t = T0 + 5000;
  for (let i = 0; i < 3; i += 1) await p.loader.refresh(); // HTTP error, unsuccessful fragment, network error
  assert.equal(p.rec.renders.length, 1, 'the old result stays on screen');
  assert.equal(p.rec.statuses.filter((s) => s.kind === 'error').length, 3);
  assert.equal(stored(storage)[0].measuredAt, T0);
  assert.equal(stored(storage)[0].html, '<p>cached</p>');
});

test('HTTP 429 blocks further manual requests until Retry-After has passed', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  const p = page({ storage, clock, responses: [
    reply({ measuredAt: T0 }), reply({ status: 429, headers: { 'retry-after': '20' } }), reply({ measuredAt: T0 + 30_000 }),
  ] });
  await p.loader.show();
  await p.loader.refresh();
  await p.loader.refresh();
  assert.equal(p.rec.calls.length, 2, 'the blocked click sends nothing');
  clock.t = T0 + 21_000;
  await p.loader.refresh();
  assert.equal(p.rec.calls.length, 3);
});

test('fast tab switching and repeated clicks share one request (single flight)', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  const p = page({ storage, clock, responses: [reply({ measuredAt: T0 })] });
  await Promise.all([p.loader.show(), p.loader.show(), p.loader.show()]);
  assert.equal(p.rec.calls.length, 1);
  const q = page({ storage: new FakeStorage(), clock, responses: [reply({ measuredAt: T0 }), reply({ measuredAt: T0 + 1 })] });
  await q.loader.show();
  await Promise.all([q.loader.refresh(), q.loader.refresh(), q.loader.refresh()]);
  assert.equal(q.rec.calls.length, 2);
});

test('entries are separated by user, target and target identity', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  seed(storage, clock, T0);
  const reads = (user, target, build = 'b1') => Cache.createStore({ storage, now: () => clock.t }).read({ user, target, build });
  assert.ok(reads('u1', 't1'));
  assert.equal(reads('u2', 't1'), null, 'another user');
  assert.equal(reads('u1', 't2'), null, 'another target');
  assert.equal(reads('u1', 't1-after-edit'), null, 'changed host or re-created target under a re-used id');
  assert.equal(reads('u1', 't1', 'b2'), null, 'an entry of another app version or build is never replayed');
  const other = page({ storage, clock, user: 'u2', responses: [reply({ measuredAt: T0 })] });
  await other.loader.show();
  assert.equal(other.rec.calls.length, 1, 'another user never gets the foreign entry');
});

test('purgeOtherUsers and clearAll remove cached diagnostics and leases only', () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  const store = Cache.createStore({ storage, now: () => clock.t });
  store.write({ user: 'u1', target: 't1', build: 'b1' }, { html: 'a', measuredAt: T0 });
  store.write({ user: 'u2', target: 't1', build: 'b1' }, { html: 'b', measuredAt: T0 });
  store.acquireLease({ user: 'u2', target: 't1', build: 'b1' }, 'x');
  storage.setItem('theme', 'dark');
  store.purgeOtherUsers('u1');
  assert.deepEqual([...storage.map.keys()].filter((k) => k.includes('.u2.')), []);
  assert.ok(store.read({ user: 'u1', target: 't1', build: 'b1' }));
  store.clearAll();
  assert.deepEqual([...storage.map.keys()], ['theme']);
});

test('logout hook clears every entry of the browser profile', () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  seed(storage, clock, T0);
  storage.setItem('unrelated', '1');
  loadModule({ localStorage: storage }).clearAll();
  assert.deepEqual([...storage.map.keys()], ['unrelated']);
});

test('corrupt, incompatible, oversized and future entries are discarded', () => {
  const clock = { t: T0 };
  const scope = { user: 'u1', target: 't1', build: 'b1' };
  const key = 'justup.traceroute.v1.e.u1.t1';
  const good = { schemaVersion: 3, build: 'b1', user: 'u1', target: 't1', measuredAt: T0, html: '<p>x</p>' };
  const cases = {
    garbage: '{not json',
    schema: JSON.stringify({ ...good, schemaVersion: 1 }),
    otherBuild: JSON.stringify({ ...good, build: 'b0' }),
    noBuild: JSON.stringify({ ...good, build: undefined }),
    foreignScope: JSON.stringify({ ...good, user: 'u2' }),
    noHtml: JSON.stringify({ ...good, html: '' }),
    badTime: JSON.stringify({ ...good, measuredAt: 'yesterday' }),
    future: JSON.stringify({ ...good, measuredAt: T0 + Cache.CLOCK_SKEW_MS + 1 }),
    tooOld: JSON.stringify({ ...good, measuredAt: T0 - Cache.RETENTION_MS - 1 }),
    oversized: JSON.stringify({ ...good, html: 'x'.repeat(Cache.MAX_HTML_CHARS + 1) }),
  };
  for (const [name, raw] of Object.entries(cases)) {
    const storage = new FakeStorage();
    storage.setItem(key, raw);
    assert.equal(Cache.createStore({ storage, now: () => clock.t }).read(scope), null, name);
    assert.equal(storage.getItem(key), null, `${name} is removed`);
  }
  const ok = new FakeStorage();
  ok.setItem(key, JSON.stringify({ ...good, measuredAt: T0 + 1000 })); // within the skew tolerance
  assert.ok(Cache.createStore({ storage: ok, now: () => clock.t }).read(scope));
});

test('a refused write (quota or broken storage) does not break the page', async () => {
  for (const storage of [new FakeStorage({ quota: 10 }), new FakeStorage({ broken: true })]) {
    const clock = { t: T0 };
    const p = page({ storage, clock, responses: [reply({ measuredAt: T0 })] });
    await p.loader.show();
    await p.loader.show();
    assert.equal(p.rec.renders.length, 1);
    assert.equal(p.rec.calls.length, 1);
    assert.equal(stored(storage).length, 0);
  }
});

test('quota pressure prunes stale entries and retries the write once', () => {
  const clock = { t: T0 };
  const storage = new FakeStorage({ quota: 700 });
  const store = Cache.createStore({ storage, now: () => clock.t });
  store.write({ user: 'u1', target: 'old', build: 'b1' }, { html: 'x'.repeat(300), measuredAt: T0 - Cache.RETENTION_MS + 10 });
  clock.t = T0 + 100; // the old entry is now past its retention
  assert.equal(store.write({ user: 'u1', target: 'new', build: 'b1' }, { html: 'y'.repeat(500), measuredAt: clock.t }), true);
  assert.equal(store.read({ user: 'u1', target: 'old', build: 'b1' }), null);
});

test('unreadable localStorage falls back to memory', async () => {
  const win = { get localStorage() { throw new Error('SecurityError'); } };
  const storage = Cache.resolveStorage(win);
  const clock = { t: T0 };
  const store = Cache.createStore({ storage, now: () => clock.t });
  assert.equal(store.write({ user: 'u1', target: 't1', build: 'b1' }, { html: 'a', measuredAt: T0 }), true);
  assert.equal(store.read({ user: 'u1', target: 't1', build: 'b1' }).html, 'a');
});

test('the entry count is bounded and the oldest entries go first', () => {
  const clock = { t: T0 };
  const storage = new FakeStorage();
  const store = Cache.createStore({ storage, now: () => clock.t });
  for (let i = 0; i < Cache.MAX_ENTRIES + 3; i += 1) store.write({ user: 'u1', target: `t${i}`, build: 'b1' }, { html: 'a', measuredAt: T0 + i });
  assert.equal(stored(storage).length, Cache.MAX_ENTRIES);
  assert.equal(store.read({ user: 'u1', target: 't0', build: 'b1' }), null);
  assert.ok(store.read({ user: 'u1', target: `t${Cache.MAX_ENTRIES + 2}`, build: 'b1' }));
});

test('a hidden tab never scans; one scan follows when it becomes visible', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  seed(storage, clock, T0);
  clock.t = T0 + TTL + 5000;
  const p = page({ storage, clock, hidden: true, responses: [reply({ measuredAt: clock.t })] });
  await p.loader.show();
  await p.loader.checkExpiry();
  assert.equal(p.rec.calls.length, 0);
  assert.equal(p.rec.renders.length, 1, 'the stale result stays visible');
  p.state.hidden = false;
  await Promise.all([p.loader.checkExpiry(), p.loader.checkExpiry()]);
  assert.deepEqual(p.rec.calls, [`${URL_}?refresh=1`]);
});

test('a tab that is not the diagnostics tab does not scan either', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  seed(storage, clock, T0);
  clock.t = T0 + TTL;
  const p = page({ storage, clock, active: false, responses: [reply({ measuredAt: clock.t })] });
  await p.loader.show();
  assert.equal(p.rec.calls.length, 0);
});

test('an open page crossing the expiry refreshes once via its timer', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  const p = page({ storage, clock, responses: [reply({ measuredAt: T0 }), reply({ measuredAt: T0 + TTL + 1000 })] });
  await p.loader.show();
  assert.equal(p.rec.timers.at(-1).ms, TTL + 1000);
  clock.t = T0 + TTL + 1000;
  await p.rec.timers.at(-1).fn();
  assert.deepEqual(p.rec.calls, [URL_, `${URL_}?refresh=1`]);
  assert.equal(p.rec.renders.length, 2);
});

test('a server answer that is not newer neither renews the cache nor repeats the scan', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  seed(storage, clock, T0);
  clock.t = T0 + TTL + 10;
  const p = page({ storage, clock, responses: [reply({ measuredAt: T0 }), reply({ status: 500 })] });
  await p.loader.show();
  assert.deepEqual(plain(p.rec.statuses.at(-1)), { kind: 'outdated' });
  assert.equal(stored(storage)[0].measuredAt, T0);
  clock.t += 60_000;
  await p.loader.checkExpiry();
  assert.equal(p.rec.calls.length, 1, 'backoff: no automatic retry loop');
  await p.loader.refresh(); // the explicit icon still works
  assert.equal(p.rec.calls.length, 2);
});

test('a failed automatic scan keeps the old result and marks it outdated', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  seed(storage, clock, T0);
  clock.t = T0 + TTL;
  const p = page({ storage, clock, responses: [reply({ status: 500 })] });
  await p.loader.show();
  assert.equal(p.rec.renders.length, 1);
  assert.deepEqual(plain(p.rec.statuses.at(-1)), { kind: 'outdated' });
  assert.equal(stored(storage)[0].measuredAt, T0);
});

test('a failed automatic scan retries after backoff without another user action', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 + TTL };
  seed(storage, { t: T0 }, T0);
  const p = page({ storage, clock, responses: [
    reply({ status: 500 }), reply({ html: '<p>refreshed</p>', measuredAt: T0 + TTL + 1000 }),
  ] });
  await p.loader.show();
  assert.deepEqual(p.rec.calls, [`${URL_}?refresh=1`]);
  assert.deepEqual(plain(p.rec.statuses.at(-1)), { kind: 'outdated' });

  const retry = p.rec.timers.at(-1);
  assert.ok(retry.ms > 0);
  clock.t += retry.ms;
  await retry.fn();

  assert.deepEqual(p.rec.calls, [`${URL_}?refresh=1`, `${URL_}?refresh=1`]);
  assert.equal(p.rec.renders.at(-1).html, '<p>refreshed</p>');
  assert.deepEqual(plain(p.rec.statuses.at(-1)), { kind: 'scanned', measuredAt: T0 + TTL + 1000 });
});

test('a response stamped in the future is clamped to the client clock', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  const p = page({ storage, clock, responses: [reply({ measuredAt: T0 + 3_600_000 })] });
  await p.loader.show();
  assert.equal(stored(storage)[0].measuredAt, T0);
  const q = page({ storage: new FakeStorage(), clock, responses: [reply({ measuredAt: T0 + 60_000 })] });
  await q.loader.show();
  assert.equal(q.rec.statuses.at(-1).measuredAt, T0);
});

test('an unsuccessful fragment (no X-Measured-At) is shown but never cached', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  const p = page({ storage, clock, responses: [reply({ html: '<p>trace returned no hops</p>' })] });
  await p.loader.show();
  assert.equal(p.rec.renders.length, 1);
  assert.equal(stored(storage).length, 0);
  assert.deepEqual(plain(p.rec.statuses.at(-1)), { kind: 'none' });
});

test('a failed first load reports the error and is retried on the next show', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  const p = page({ storage, clock, responses: [reply({ status: 502 }), reply({ measuredAt: T0 })] });
  await p.loader.show();
  assert.equal(p.rec.loadErrors.length, 1);
  await p.loader.show();
  assert.equal(p.rec.calls.length, 2);
  assert.equal(p.rec.renders.length, 1);
});

test('a session redirect is forwarded and stores nothing', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  const p = page({ storage, clock, responses: [reply({ headers: { 'hx-redirect': '/ui/login' } }), reply({ headers: { 'hx-redirect': '//evil.example' }, status: 500 })] });
  await p.loader.show();
  assert.deepEqual(p.rec.redirects, ['/ui/login']);
  assert.equal(stored(storage).length, 0);
  assert.equal(p.rec.renders.length, 0);
});

test('an off-origin redirect target is never followed', async () => {
  for (const target of ['//evil.example', '/\\evil.example', 'https://evil.example/']) {
    const storage = new FakeStorage(); const clock = { t: T0 };
    const p = page({ storage, clock, responses: [reply({ headers: { 'hx-redirect': target }, status: 500 })] });
    await p.loader.show();
    assert.deepEqual(p.rec.redirects, [], target);
  }
});
test('two tabs with Web Locks coalesce the automatic scan into one request', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  seed(storage, clock, T0);
  clock.t = T0 + TTL + 1;
  let tail = Promise.resolve();
  const locks = { request: (_name, fn) => { const run = tail.then(fn); tail = run.catch(() => {}); return run; } };
  const a = page({ storage, clock, locks, responses: [reply({ measuredAt: clock.t })] });
  const b = page({ storage, clock, locks, responses: [reply({ measuredAt: clock.t })] });
  await Promise.all([a.loader.show(), b.loader.show()]);
  assert.equal(a.rec.calls.length + b.rec.calls.length, 1);
  assert.equal(b.rec.renders.at(-1).html, a.rec.renders.at(-1).html, 'the waiting tab shows the fresh result');
});

test('without Web Locks a storage lease keeps the second tab from scanning', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  seed(storage, clock, T0);
  clock.t = T0 + TTL + 1;
  const store = Cache.createStore({ storage, now: () => clock.t });
  let release;
  const gate = new Promise((r) => { release = r; });
  const fetches = [];
  const holder = Cache.createLoader({
    url: URL_, scope: { user: 'u1', target: 't1', build: 'b1' }, store, now: () => clock.t, owner: 'holder',
    fetchFn: async (u) => { fetches.push(u); await gate; return reply({ measuredAt: clock.t }); },
    render: () => {}, onRedirect: () => {}, setTimer: () => 1, clearTimer: () => {},
  });
  const running = holder.show();
  const second = page({ storage, clock, responses: [reply({ measuredAt: clock.t })] });
  await second.loader.show();
  assert.equal(second.rec.calls.length, 0, 'the lease holder is the only scanner');
  release();
  await running;
  assert.equal(fetches.length, 1);
  assert.ok(second.rec.timers.length >= 1, 'a re-check is scheduled for after the lease');
});

test('an automatic scan picks up a fresher result written by another tab without a request', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  seed(storage, clock, T0);
  const p = page({ storage, clock });
  await p.loader.show();
  clock.t = T0 + TTL + 10;
  seed(storage, clock, T0 + TTL + 5, {});
  await p.loader.checkExpiry();
  assert.equal(p.rec.calls.length, 0);
  assert.equal(p.rec.renders.length, 2);
});

test('without Web Locks a second tab on its first load adopts the result of the first tab', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  let release;
  const gate = new Promise((r) => { release = r; });
  const tabA = Cache.createLoader({
    url: URL_, scope: { user: 'u1', target: 't1', build: 'b1' }, store: Cache.createStore({ storage, now: () => clock.t }),
    now: () => clock.t, owner: 'A', render: () => {}, onRedirect: () => {}, setTimer: () => 1, clearTimer: () => {},
    fetchFn: async () => { await gate; return reply({ html: '<p>from A</p>', measuredAt: T0 }); },
  });
  const running = tabA.show();
  const tabB = page({ storage, clock });
  await tabB.loader.show(); // lease held by A: no request, still loading
  assert.equal(tabB.rec.calls.length, 0);
  assert.equal(tabB.loader.isLoaded(), false);
  release();
  await running;
  clock.t += 31_000;
  await tabB.rec.timers.at(-1).fn(); // the fallback timer, or the storage event, rechecks
  assert.equal(tabB.rec.calls.length, 0, 'no trace request of its own');
  assert.equal(tabB.rec.renders.at(-1).html, '<p>from A</p>');
  assert.equal(tabB.loader.isLoaded(), true);
});

// Geo key: a changed IXP data set must never replay locations from the old one.
const seedGeo = (storage, clock, geo, measuredAt, html = '<p>old locations</p>') =>
  Cache.createStore({ storage, now: () => clock.t }).write({ user: 'u1', target: 't1', build: 'b1', geo }, { html, measuredAt });
test('an entry is stored with the geo key of the page and replayed while the key is unchanged', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  await page({ storage, clock, geo: 'g1', responses: [reply({ measuredAt: T0 - 1000 })] }).loader.show();
  assert.equal(stored(storage)[0].geo, 'g1');
  clock.t = T0 + 60_000;
  const reloaded = page({ storage, clock, geo: 'g1' });
  await reloaded.loader.show();
  assert.equal(reloaded.rec.calls.length, 0);
  assert.equal(plain(reloaded.rec.renders[0]).html, '<p>route</p>');
});
test('a changed geo key drops the entry and loads the re-rendered result without a forced scan', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  seedGeo(storage, clock, 'g1', T0 - 3_600_000);
  const reloaded = page({ storage, clock, geo: 'g2', responses: [reply({ html: '<p>new locations</p>', measuredAt: T0 - 3_600_000 })] });
  await reloaded.loader.show();
  assert.deepEqual(reloaded.rec.calls, [URL_]); // plain request: the server answers from its stored route
  assert.equal(plain(reloaded.rec.renders.at(-1)).html, '<p>new locations</p>');
  const [entry] = stored(storage);
  assert.equal(entry.geo, 'g2');
  assert.equal(entry.measuredAt, T0 - 3_600_000); // the measurement time is the original one
  const again = page({ storage, clock, geo: 'g2' });
  await again.loader.show();
  assert.equal(again.rec.calls.length, 0);
});
test('an entry without a geo key is not replayed on a page that carries one', async () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  seedGeo(storage, clock, undefined, T0 - 1000);
  const reloaded = page({ storage, clock, geo: 'g1', responses: [reply({ html: '<p>fresh</p>', measuredAt: T0 - 1000 })] });
  await reloaded.loader.show();
  assert.equal(reloaded.rec.calls.length, 1);
  assert.equal(plain(reloaded.rec.renders.at(-1)).html, '<p>fresh</p>');
});
test('a malformed geo key is refused instead of being stored', () => {
  const storage = new FakeStorage(); const clock = { t: T0 };
  const store = Cache.createStore({ storage, now: () => clock.t });
  assert.equal(store.write({ user: 'u1', target: 't1', build: 'b1', geo: 'bad key!' }, { html: '<p>x</p>', measuredAt: T0 }), false);
  assert.equal(stored(storage).length, 0);
});
