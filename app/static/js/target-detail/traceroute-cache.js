//
// app/static/js/target-detail/traceroute-cache.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Browser-side 24 h cache for the network-diagnostics fragment of a target.
 *
 * - Decision first: a valid entry is rendered without any request to the traceroute endpoint.
 * - An expired entry triggers exactly one automatic scan (`refresh=1`, so the server's
 *   five-minute cache cannot answer it), only while the page is visible and the tab is active.
 * - The 24 h run from the measurement time the server reports (`X-Measured-At`), never from
 *   the last display. A failed scan keeps the previous entry and its timestamp.
 *
 * Storage: localStorage (persistent across reload and browser restart; IndexedDB would buy
 * nothing for a handful of small strings). The cache holds the server-rendered fragment HTML
 * because the fragment endpoint returns nothing else; it is re-inserted through the same inert
 * DOMParser path as a live response. Entries are separated by user and target scope (opaque
 * server-issued keys), shape-checked, size-capped and pruned. No auth data is ever stored.
 * Without usable storage the module falls back to an in-memory store.
 *
 * Clock: `measuredAt` is server time compared with the client clock. A response stamp up to
 * CLOCK_SKEW_MS in the future is clamped to the client clock; a larger one is replaced by the
 * client clock. A stored entry stamped further ahead than the tolerance is discarded.
 *
 * @module traceroute-cache
 */
(function (root) {
	'use strict';
	const TTL_MS = 86_400_000;
	const SCHEMA_VERSION = 2;
	const KEY_PREFIX = 'justup.traceroute.v1.';
	const ENTRY_PREFIX = `${KEY_PREFIX}e.`;
	const LEASE_PREFIX = `${KEY_PREFIX}l.`;
	const MAX_ENTRIES = 12;
	const MAX_HTML_CHARS = 262_144;
	const RETENTION_MS = 7 * TTL_MS; // an expired entry stays visible as "outdated" this long
	const CLOCK_SKEW_MS = 300_000;
	const LEASE_MS = 30_000;
	const AUTO_RETRY_BACKOFF_MS = 300_000;
	const RATE_LIMIT_MESSAGE = 'A measurement was just started. Please wait a few seconds and try again.';
	const SCAN_FAILED_MESSAGE = 'The measurement failed.';
	const ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

	const isId = (value) => typeof value === 'string' && ID_PATTERN.test(value);
	const isScope = (scope) => Boolean(scope) && isId(scope.user) && isId(scope.target) && isId(scope.build);

	function createMemoryStorage() {
		const map = new Map();
		return {
			getItem: (key) => (map.has(key) ? map.get(key) : null),
			setItem: (key, value) => { map.set(key, String(value)); },
			removeItem: (key) => { map.delete(key); },
			key: (index) => [...map.keys()][index] ?? null,
			get length() { return map.size; },
		};
	}

	/** localStorage when readable, otherwise an in-memory store (access itself may throw). */
	function resolveStorage(win) {
		try {
			const storage = win.localStorage;
			storage.getItem(`${KEY_PREFIX}probe`);
			return storage;
		} catch {
			return createMemoryStorage();
		}
	}

	function createStore({ storage, now = Date.now }) {
		const entryKey = (scope) => `${ENTRY_PREFIX}${scope.user}.${scope.target}`;
		const leaseKey = (scope) => `${LEASE_PREFIX}${scope.user}.${scope.target}`;
		const attempt = (fn, fallback) => {
			try { return fn(); } catch { return fallback; }
		};
		const keysWith = (prefix) => attempt(() => {
			const found = [];
			for (let i = 0; i < storage.length; i += 1) {
				const key = storage.key(i);
				if (key && key.startsWith(prefix)) found.push(key);
			}
			return found;
		}, []);
		const remove = (key) => attempt(() => storage.removeItem(key));
		/** Validated entry for `key`, or null; invalid, foreign-schema and over-aged data is removed. */
		const parseEntry = (key, scope) => {
			const raw = attempt(() => storage.getItem(key), null);
			if (raw === null) return null;
			let entry = null;
			if (raw.length <= MAX_HTML_CHARS + 1024) entry = attempt(() => JSON.parse(raw), null);
			const t = now();
			const valid = Boolean(entry) && entry.schemaVersion === SCHEMA_VERSION
				&& isId(entry.user) && isId(entry.target) && isId(entry.build)
				&& (!scope || (entry.user === scope.user && entry.target === scope.target && entry.build === scope.build))
				&& Number.isFinite(entry.measuredAt) && entry.measuredAt <= t + CLOCK_SKEW_MS
				&& t - entry.measuredAt <= RETENTION_MS
				&& typeof entry.html === 'string' && entry.html.length > 0 && entry.html.length <= MAX_HTML_CHARS;
			if (!valid) {
				remove(key);
				return null;
			}
			return entry;
		};

		function read(scope) {
			if (!isScope(scope)) return null;
			const entry = parseEntry(entryKey(scope), scope);
			if (!entry) return null;
			return { html: entry.html, measuredAt: entry.measuredAt, expired: now() - entry.measuredAt >= TTL_MS };
		}

		function prune() {
			const t = now();
			const live = [];
			for (const key of keysWith(ENTRY_PREFIX)) {
				const entry = parseEntry(key, null);
				if (entry) live.push({ key, measuredAt: entry.measuredAt });
			}
			live.sort((a, b) => a.measuredAt - b.measuredAt);
			while (live.length > MAX_ENTRIES) remove(live.shift().key);
			for (const key of keysWith(LEASE_PREFIX)) {
				const lease = attempt(() => JSON.parse(storage.getItem(key)), null);
				if (!lease || !(lease.until > t) || lease.until > t + LEASE_MS) remove(key);
			}
		}

		/** Stores a successful measurement; returns false when the browser refuses (quota, disabled). */
		function write(scope, { html, measuredAt }) {
			if (!isScope(scope) || typeof html !== 'string' || !html || html.length > MAX_HTML_CHARS
				|| !Number.isFinite(measuredAt)) return false;
			const payload = JSON.stringify({
				schemaVersion: SCHEMA_VERSION, user: scope.user, target: scope.target, build: scope.build, measuredAt, html,
			});
			for (let tries = 0; tries < 2; tries += 1) {
				try {
					storage.setItem(entryKey(scope), payload);
					if (keysWith(ENTRY_PREFIX).length > MAX_ENTRIES) prune();
					return true;
				} catch {
					prune(); // quota: free stale data once, then give up
				}
			}
			return false;
		}

		const purge = (predicate) => {
			for (const prefix of [ENTRY_PREFIX, LEASE_PREFIX]) {
				for (const key of keysWith(prefix)) {
					if (predicate(key.slice(prefix.length).split('.')[0])) remove(key);
				}
			}
		};

		function acquireLease(scope, owner) {
			if (!isScope(scope)) return false;
			const key = leaseKey(scope);
			const held = attempt(() => JSON.parse(storage.getItem(key)), null);
			if (held && held.owner !== owner && held.until > now()) return false;
			try {
				storage.setItem(key, JSON.stringify({ owner, until: now() + LEASE_MS }));
			} catch {
				return true; // storage refuses writes: nothing to coordinate with, scan like a single tab
			}
			const stored = attempt(() => JSON.parse(storage.getItem(key)), null);
			return !stored || stored.owner === owner;
		}

		return {
			read,
			write,
			prune,
			acquireLease,
			releaseLease: (scope, owner) => {
				const held = attempt(() => JSON.parse(storage.getItem(leaseKey(scope))), null);
				if (held && held.owner === owner) remove(leaseKey(scope));
			},
			/** Drops every entry that does not belong to the current user (user switch, shared browser). */
			purgeOtherUsers: (user) => purge((entryUser) => entryUser !== user),
			clearAll: () => purge(() => true),
		};
	}

	class ScanError extends Error {
		constructor(message, { status = 0, retryAfterMs = 0 } = {}) {
			super(message);
			this.name = 'ScanError';
			this.status = status;
			this.retryAfterMs = retryAfterMs;
		}
	}

	/**
	 * Single-flight loader: owns the "cache or request" decision for one target scope.
	 * Rendering and DOM state are injected, so the module itself never touches the document.
	 */
	function createLoader(options) {
		const {
			url, scope, store, fetchFn, render, onRedirect,
			now = Date.now,
			locks = null,
			isHidden = () => false,
			isActive = () => true,
			setBusy = () => {},
			setStatus = () => {},
			setLoading = () => {},
			setLoadError = () => {},
			setTimer = (fn, ms) => setTimeout(fn, ms),
			clearTimer = (id) => clearTimeout(id),
			owner = Math.random().toString(36).slice(2),
		} = options;
		const lockName = `justup-traceroute:${scope.user}:${scope.target}`;
		let loaded = false;
		let renderedAt = null;
		let inFlight = null;
		let timer = null;
		let autoBlockedUntil = 0;
		let manualBlockedUntil = 0;

		const display = (html, measuredAt) => {
			render({ html, measuredAt });
			loaded = true;
			renderedAt = measuredAt;
			setStatus(measuredAt === null ? { kind: 'none' } : { kind: 'scanned', measuredAt });
		};

		const scheduleExpiry = () => {
			clearTimer(timer);
			timer = null;
			if (renderedAt === null) return;
			const delay = Math.max(renderedAt + TTL_MS - now(), 0) + 1000;
			timer = setTimer(() => { timer = null; return checkExpiry(); }, delay);
		};

		/** One fragment request; resolves to {html, measuredAt|null} or {redirected: true}. */
		async function request(refresh) {
			const response = await fetchFn(refresh ? `${url}?refresh=1` : url, { headers: { 'HX-Request': 'true' } });
			const redirect = response.headers.get('HX-Redirect');
			if (redirect && redirect.startsWith('/') && !redirect.startsWith('//')) {
				onRedirect(redirect);
				return { redirected: true };
			}
			if (response.status === 429) {
				const seconds = Number.parseInt(response.headers.get('Retry-After'), 10);
				throw new ScanError(RATE_LIMIT_MESSAGE, {
					status: 429, retryAfterMs: Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : 0,
				});
			}
			if (!response.ok) {
				throw new ScanError(`Network diagnostics could not be loaded (${response.status}).`, { status: response.status });
			}
			const html = await response.text();
			const header = response.headers.get('X-Measured-At');
			let measuredAt = typeof header === 'string' && /^\d{1,15}$/.test(header) ? Number(header) : null;
			if (measuredAt !== null) {
				const t = now();
				measuredAt = measuredAt > t + CLOCK_SKEW_MS ? t : Math.min(measuredAt, t);
			}
			return { html, measuredAt };
		}

		/** Applies a response; only a measurement newer than the displayed one replaces anything. */
		function commit(result) {
			if (result.measuredAt === null || result.measuredAt <= (renderedAt ?? -1)) return false;
			store.write(scope, { html: result.html, measuredAt: result.measuredAt });
			display(result.html, result.measuredAt);
			scheduleExpiry();
			return true;
		}

		async function withScanLock(task) {
			let ran = false;
			const guarded = async () => { ran = true; return task(); };
			if (locks && typeof locks.request === 'function') {
				try {
					return await locks.request(lockName, guarded);
				} catch (error) {
					if (ran) throw error;
				}
			}
			if (!store.acquireLease(scope, owner)) {
				// Another tab is measuring: look again once its lease ran out.
				clearTimer(timer);
				timer = setTimer(() => { timer = null; return recheck(); }, LEASE_MS + 1000);
				return undefined;
			}
			try {
				return await task();
			} finally {
				store.releaseLease(scope, owner);
			}
		}

		const fresh = (entry) => Boolean(entry) && !entry.expired && entry.measuredAt > (renderedAt ?? -1);

		async function autoScan() {
			if (inFlight) return inFlight;
			if (isHidden() || !isActive()) return undefined;
			if (now() < autoBlockedUntil) {
				setStatus({ kind: 'outdated' });
				return undefined;
			}
			inFlight = withScanLock(async () => {
				const current = store.read(scope);
				if (fresh(current)) { // another tab already measured
					display(current.html, current.measuredAt);
					scheduleExpiry();
					return;
				}
				setBusy(true);
				try {
					const result = await request(true);
					if (result.redirected) return;
					if (!commit(result)) throw new ScanError(SCAN_FAILED_MESSAGE);
				} catch (error) {
					autoBlockedUntil = now() + Math.max(error.retryAfterMs || 0, AUTO_RETRY_BACKOFF_MS);
					setStatus({ kind: 'outdated' });
				} finally {
					setBusy(false);
				}
			}).finally(() => { inFlight = null; });
			return inFlight;
		}

		function checkExpiry() {
			if (!loaded || inFlight) return undefined;
			const entry = store.read(scope);
			if (fresh(entry)) display(entry.html, entry.measuredAt); // newer result from another tab
			if (renderedAt === null) return undefined;
			if (now() - renderedAt < TTL_MS) {
				scheduleExpiry();
				return undefined;
			}
			return autoScan();
		}

		/** Another tab may have stored a result: a page still without one retries its first load. */
		function recheck() {
			if (inFlight) return inFlight;
			return loaded ? checkExpiry() : show();
		}

		function firstLoad() {
			setLoading();
			inFlight = withScanLock(async () => {
				const current = store.read(scope);
				if (current) { // measured by another tab while this one waited
					display(current.html, current.measuredAt);
					scheduleExpiry();
					return;
				}
				try {
					const result = await request(false);
					if (result.redirected) return;
					if (!commit(result)) display(result.html, null); // unsuccessful scan: shown, never cached
				} catch (error) {
					setLoadError(error instanceof Error ? error.message : SCAN_FAILED_MESSAGE);
				}
			}).finally(() => { inFlight = null; });
			return inFlight;
		}

		/** Diagnostics tab shown (or page load on that tab). No DOM is rebuilt once loaded. */
		function show() {
			if (inFlight) return inFlight;
			if (loaded) return checkExpiry();
			const entry = store.read(scope);
			if (!entry) return firstLoad();
			display(entry.html, entry.measuredAt);
			scheduleExpiry();
			return entry.expired ? autoScan() : undefined;
		}

		/** Explicit "Run new scan": always `refresh=1`; the previous result survives a failure. */
		function refresh() {
			if (inFlight) return inFlight;
			if (now() < manualBlockedUntil) {
				setStatus({ kind: 'error', message: RATE_LIMIT_MESSAGE });
				return undefined;
			}
			inFlight = (async () => {
				setBusy(true);
				try {
					const result = await request(true);
					if (result.redirected) return;
					if (!commit(result)) throw new ScanError(SCAN_FAILED_MESSAGE);
				} catch (error) {
					if (error.status === 429) manualBlockedUntil = now() + (error.retryAfterMs || 15_000);
					setStatus({ kind: 'error', message: error instanceof Error ? error.message : SCAN_FAILED_MESSAGE });
				} finally {
					setBusy(false);
				}
			})().finally(() => { inFlight = null; });
			return inFlight;
		}

		return {
			show,
			refresh,
			checkExpiry,
			recheck,
			isLoaded: () => loaded,
			destroy: () => { clearTimer(timer); timer = null; },
		};
	}

	root.TracerouteCache = {
		TTL_MS, SCHEMA_VERSION, KEY_PREFIX, MAX_ENTRIES, MAX_HTML_CHARS, CLOCK_SKEW_MS, RETENTION_MS,
		createStore, createLoader, createMemoryStorage, resolveStorage,
		/** Logout hook: removes every cached diagnostic of this browser profile. */
		clearAll: () => createStore({ storage: resolveStorage(root) }).clearAll(),
	};
}(typeof window !== 'undefined' ? window : globalThis));
