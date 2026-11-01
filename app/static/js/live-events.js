//
// app/static/js/live-events.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * One Server-Sent Events connection per tab (/ui/events), shared by every UI component.
 *
 * The stream carries invalidation hints only; components reload through the existing REST
 * endpoints. Two states are kept apart on purpose: whether the SSE stream works and whether the
 * server is reachable. A dropped stream alone never reports "offline" (proxies may block
 * streaming); only failing HTTP health checks do.
 *
 * States: connected | reconnecting | fallback | offline | authentication-lost
 */
(function (root) {
	'use strict';

	var HEARTBEAT_TIMEOUT_MS = 45000;
	var FALLBACK_MS = 15000;
	var SAFETY_RESYNC_MS = 300000;
	var COALESCE_MS = 300;
	var FALLBACK_AFTER_ATTEMPTS = 3;
	var OFFLINE_AFTER_PROBE_FAILURES = 2;
	var MAX_BACKOFF_MS = 30000;
	var EVENT_TYPES = [
		'target.measurement', 'target.status', 'alert.changed', 'notification.changed',
		'target.changed', 'system.heartbeat', 'system.resync', 'system.auth-lost'
	];

	// Maps an event to the DOM events that HTMX elements listen for (hx-trigger "... from:body").
	function domNames(type, data) {
		var id = data && data.target_id;
		var names = [];
		if (type === 'target.status' || type === 'target.measurement') {
			names.push('justup-status-summary', 'justup-target-list');
			if (id) names.push('justup-target-' + id);
		} else if (type === 'alert.changed') {
			names.push('justup-status-summary');
			if (id) names.push('justup-target-' + id);
		} else if (type === 'target.changed') {
			names.push('justup-target-list', 'justup-status-summary');
			if (id) names.push('justup-target-' + id);
		}
		return names;
	}

	/**
	 * @param {object} deps Injected environment (EventSource, timers, probes) so the state machine is testable.
	 */
	function createLiveClient(deps) {
		var state = 'reconnecting';
		var es = null;
		var closed = false;
		var recovering = false;
		var attempts = 0;
		var probeFailures = 0;
		var probing = false;
		var hadProblem = false;
		var version = null;
		var subs = [];
		var listeners = [];
		var timers = { heartbeat: null, step: null, fallback: null, safety: null, dom: null };
		var domPending = Object.create(null);
		var domDirty = false;

		function setTimer(name, fn, ms) {
			clearTimer(name);
			timers[name] = deps.setTimer(function () { timers[name] = null; fn(); }, ms);
		}
		function clearTimer(name) {
			if (timers[name] !== null) { deps.clearTimer(timers[name]); timers[name] = null; }
		}

		function setState(next) {
			if (next === state) return;
			var prev = state;
			state = next;
			if (next === 'reconnecting' || next === 'fallback') startFallbackTicks(); else clearTimer('fallback');
			listeners.slice().forEach(function (fn) {
				try { fn(next, prev); } catch (e) { /* a listener must not break the client */ }
			});
		}

		// ── Component fan-out ─────────────────────────────────────────────────
		function flushSub(sub) {
			sub.timer = null;
			var batch = sub.pending;
			sub.pending = [];
			if (!sub.active || !batch.length) return;
			try { sub.handler(batch); } catch (e) { /* isolate subscriber failures */ }
		}

		function deliver(type, data) {
			var event = { type: type, data: data || {} };
			subs.slice().forEach(function (sub) {
				if (!sub.active || sub.types.indexOf(type) === -1) return;
				if (deps.isHidden()) { sub.dirty = true; return; }
				sub.pending.push(event);
				if (sub.timer === null) sub.timer = deps.setTimer(function () { flushSub(sub); }, sub.coalesceMs);
			});
			bridge(type, data);
		}

		function bridge(type, data) {
			var names = domNames(type, data);
			if (!names.length) return;
			if (deps.isHidden()) { domDirty = true; return; }
			names.forEach(function (n) { domPending[n] = true; });
			if (timers.dom === null) setTimer('dom', flushDom, COALESCE_MS);
		}
		function flushDom() {
			var names = Object.keys(domPending);
			domPending = Object.create(null);
			names.forEach(function (n) { deps.dispatchDom(n); });
		}

		function resyncAll() {
			subs.slice().forEach(function (sub) {
				if (!sub.active || !sub.resync) return;
				if (deps.isHidden()) { sub.dirty = true; return; }
				try { sub.resync(); } catch (e) { /* isolate subscriber failures */ }
			});
			if (deps.isHidden()) domDirty = true; else deps.dispatchDom('justup-live-sync');
		}

		function startFallbackTicks() {
			setTimer('fallback', function () {
				if (state === 'reconnecting' || state === 'fallback') {
					if (!deps.isHidden()) resyncAll();
					startFallbackTicks();
				}
			}, FALLBACK_MS);
		}

		function startSafetyTimer() {
			setTimer('safety', function () {
				if (state === 'connected') { if (!deps.isHidden()) resyncAll(); startSafetyTimer(); }
			}, SAFETY_RESYNC_MS);
		}

		// ── SSE connection ────────────────────────────────────────────────────
		function armHeartbeat() {
			setTimer('heartbeat', function () { teardown(); lost(); }, HEARTBEAT_TIMEOUT_MS);
		}

		function teardown() {
			clearTimer('heartbeat');
			if (es) {
				es.onerror = null;
				es.close();
				es = null;
			}
		}

		function connect() {
			if (closed || es || !deps.EventSource || state === 'authentication-lost') return;
			var source;
			try { source = new deps.EventSource('/ui/events'); } catch (e) { lost(); return; }
			es = source;
			source.onerror = function () { if (es === source) { teardown(); lost(); } };
			EVENT_TYPES.forEach(function (type) {
				source.addEventListener(type, function (ev) {
					if (es !== source) return;
					var data = {};
					try { data = ev.data ? JSON.parse(ev.data) : {}; } catch (e) { data = {}; }
					onMessage(type, data);
				});
			});
			armHeartbeat();
		}

		function onMessage(type, data) {
			armHeartbeat();
			if (state !== 'connected') onConnected();
			if (type === 'system.heartbeat') {
				if (data.version) {
					if (version && version !== data.version) { deps.reload(); return; }
					version = data.version;
				}
				return;
			}
			if (type === 'system.resync') { resyncAll(); return; }
			if (type === 'system.auth-lost') { teardown(); lost(); return; }
			deliver(type, data);
		}

		function onConnected() {
			attempts = 0;
			probeFailures = 0;
			clearTimer('step');
			recovering = false;
			var recoveredFromProblem = hadProblem;
			hadProblem = false;
			setState('connected');
			startSafetyTimer();
			// Events may have been missed while the stream was down: reload instead of replaying.
			if (recoveredFromProblem) resyncAll();
		}

		function lost() {
			if (closed || state === 'authentication-lost') return;
			hadProblem = true;
			clearTimer('safety');
			if (state === 'connected') setState('reconnecting');
			recover();
		}

		// ── Recovery: HTTP health decides "offline", never the stream alone ──
		function probe() {
			if (probing) return Promise.resolve(null);
			probing = true;
			return Promise.resolve().then(function () { return deps.fetchHealth(); }).then(function (ok) {
				return !!ok;
			}, function () { return false; }).then(function (ok) {
				probing = false;
				return ok;
			});
		}

		function backoff() {
			return Math.min(1000 * Math.pow(2, Math.max(0, attempts - 1)), MAX_BACKOFF_MS);
		}

		function recover() {
			if (recovering || closed) return;
			recovering = true;
			step();
		}

		function step() {
			if (closed || state === 'connected' || state === 'authentication-lost') { recovering = false; return; }
			probe().then(function (ok) {
				if (closed || state === 'connected' || state === 'authentication-lost') { recovering = false; return; }
				if (ok === null) { setTimer('step', step, 1000); return; }
				if (!ok) {
					probeFailures++;
					if (probeFailures >= OFFLINE_AFTER_PROBE_FAILURES) setState('offline');
					setTimer('step', step, Math.min(1500 * Math.pow(1.4, probeFailures), 10000));
					return;
				}
				// HTTP works: the server is reachable, so only the stream is in doubt.
				var wasOffline = state === 'offline';
				probeFailures = 0;
				return Promise.resolve().then(function () { return deps.fetchSession(); }).catch(function () { return 0; })
					.then(function (status) {
						if (closed) return;
						if (status === 401 || status === 403) { authLost(); return; }
						attempts++;
						setState(attempts >= FALLBACK_AFTER_ATTEMPTS || !deps.EventSource ? 'fallback' : 'reconnecting');
						if (wasOffline) resyncAll();
						connect();
						setTimer('step', step, backoff());
					});
			});
		}

		function authLost() {
			teardown();
			recovering = false;
			clearTimer('step');
			clearTimer('fallback');
			clearTimer('safety');
			setState('authentication-lost');
			deps.goLogin();
		}

		// ── Public API ────────────────────────────────────────────────────────
		return {
			start: function () {
				if (!deps.EventSource) { hadProblem = true; setState('fallback'); recover(); return; }
				connect();
			},
			state: function () { return state; },
			onState: function (fn) {
				listeners.push(fn);
				return function () { listeners = listeners.filter(function (f) { return f !== fn; }); };
			},
			/**
			 * @param {string[]} types Event types to receive.
			 * @param {function(object[])} handler Called with the coalesced batch of events.
			 * @param {{resync: function, coalesceMs: number}} [opts] resync reloads everything (reconnect, fallback, tab visible).
			 * @returns {function} unsubscribe
			 */
			subscribe: function (types, handler, opts) {
				var sub = {
					types: types, handler: handler, resync: opts && opts.resync,
					coalesceMs: (opts && opts.coalesceMs) || COALESCE_MS, pending: [], timer: null, dirty: false, active: true
				};
				subs.push(sub);
				return function () {
					sub.active = false;
					if (sub.timer !== null) { deps.clearTimer(sub.timer); sub.timer = null; }
					subs = subs.filter(function (s) { return s !== sub; });
				};
			},
			notifyVisible: function () {
				if (deps.isHidden()) return;
				var needsDom = domDirty;
				domDirty = false;
				subs.slice().forEach(function (sub) {
					if (!sub.dirty) return;
					sub.dirty = false;
					if (sub.resync) { try { sub.resync(); } catch (e) { /* isolate */ } }
				});
				if (needsDom) deps.dispatchDom('justup-live-sync');
			},
			notifyBrowserOffline: function () { teardown(); lost(); },
			notifyBrowserOnline: function () {
				if (state === 'connected') return;
				attempts = 0;
				clearTimer('step');
				recovering = false;
				recover();
			},
			// An HTTP transport failure only matters while the stream cannot vouch for the server.
			reportHttpFailure: function () { if (state !== 'connected') recover(); },
			close: function () {
				closed = true;
				teardown();
				Object.keys(timers).forEach(clearTimer);
				subs.forEach(function (sub) { sub.active = false; if (sub.timer !== null) deps.clearTimer(sub.timer); });
				subs = [];
				listeners = [];
			}
		};
	}

	root.JustUpLive = { createLiveClient: createLiveClient, client: null };

	if (!root.document || !root.document.getElementById || !root.document.getElementById('app-shell')) return;
	var doc = root.document;

	function timedFetch(url, ms) {
		var controller = new root.AbortController();
		var id = root.setTimeout(function () { controller.abort(); }, ms);
		return root.fetch(url, { cache: 'no-store', credentials: 'same-origin', signal: controller.signal })
			.then(function (resp) { root.clearTimeout(id); return resp; }, function (err) { root.clearTimeout(id); throw err; });
	}

	var client = createLiveClient({
		EventSource: root.EventSource,
		setTimer: function (fn, ms) { return root.setTimeout(fn, ms); },
		clearTimer: function (id) { root.clearTimeout(id); },
		isHidden: function () { return doc.hidden; },
		dispatchDom: function (name) { doc.body.dispatchEvent(new root.CustomEvent(name)); },
		fetchHealth: function () { return timedFetch('/health', 5000).then(function (r) { return r.ok; }); },
		fetchSession: function () { return timedFetch('/ui/events/session', 5000).then(function (r) { return r.status; }); },
		goLogin: function () { root.location.href = '/ui/login'; },
		reload: function () { root.location.reload(); }
	});
	root.JustUpLive.client = client;

	var status = doc.getElementById('live-status');
	var STATUS_TEXT = {
		reconnecting: 'Live updates interrupted. Reconnecting.',
		fallback: 'Live updates unavailable. Refreshing periodically.',
		connected: 'Live updates restored.'
	};
	var announced = false;
	client.onState(function (next) {
		if (!status) return;
		if (next === 'connected' && !announced) return;
		if (STATUS_TEXT[next]) { status.textContent = STATUS_TEXT[next]; announced = next !== 'connected'; }
	});

	doc.addEventListener('visibilitychange', function () { client.notifyVisible(); });
	root.addEventListener('offline', function () { client.notifyBrowserOffline(); });
	root.addEventListener('online', function () { client.notifyBrowserOnline(); });
	root.addEventListener('pagehide', function () { client.close(); });
	// A page restored from the back/forward cache lost its stream; reload instead of showing stale data.
	root.addEventListener('pageshow', function (e) { if (e.persisted) root.location.reload(); });
	function onHttpError(evt) {
		var xhr = evt && evt.detail && evt.detail.xhr;
		if (!xhr || xhr.status === 0) client.reportHttpFailure();
	}
	doc.addEventListener('htmx:sendError', onHttpError);
	client.start();
})(typeof window !== 'undefined' ? window : globalThis);
