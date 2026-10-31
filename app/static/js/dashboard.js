//
// app/static/js/dashboard.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

(function () {
	'use strict';

	// Defensive check: Ensure apiFetch is available
	if (typeof window.apiFetch !== 'function') {
		console.error('apiFetch is not defined. Ensure base.html loads the API utility.');
		return;
	}

	// ─────────────────────────────────────────────────────────────
	// Configuration Constants
	// ─────────────────────────────────────────────────────────────
	const LOOKBACK_WINDOW_MS = 24 * 60 * 60 * 1000; // 24h — time window for downtime stats
	const POLL_INTERVAL_MS = 30 * 1000;         // 30s — polling interval when tab is visible

	// ─────────────────────────────────────────────────────────────
	// Utility Functions
	// ─────────────────────────────────────────────────────────────

	function formatDuration(ms) {
		if (ms <= 0) return '0s';
		if (ms < 1000) return '< 1s';
		const seconds = Math.floor(ms / 1000);
		const minutes = Math.floor(seconds / 60);
		const hours = Math.floor(minutes / 60);

		if (hours > 0) {
			const remainingMinutes = minutes % 60;
			return remainingMinutes > 0 ? (hours + 'h ' + remainingMinutes + 'm') : (hours + 'h');
		}
		if (minutes > 0) {
			const remainingSeconds = seconds % 60;
			return remainingSeconds > 0 ? (minutes + 'm ' + remainingSeconds + 's') : (minutes + 'm');
		}
		return seconds + 's';
	}

	/**
	 * Create a table cell displaying service downtime.
	 * @param {Object} stats - Stats object with totalDowntimeMs property
	 * @returns {HTMLTableCellElement}
	 */
	function createServiceCell(stats) {
		const td = document.createElement('td');
		td.className = 'text-end';
		if (stats.totalDowntimeMs > 0) {
			td.className += ' text-danger fw-semibold';
			td.textContent = formatDuration(stats.totalDowntimeMs);
		} else {
			td.className += ' text-body-secondary';
			td.textContent = '—';
		}
		return td;
	}

	// ─────────────────────────────────────────────────────────────
	// Main Data Loading
	// ─────────────────────────────────────────────────────────────

	let downtimeLoading = false;

	function byId(id) {
		return document.getElementById(id);
	}

	// Exactly one of the content states is visible; stale rows never survive a failure.
	function showState(name) {
		['table-container', 'success', 'empty', 'error', 'loading'].forEach(function (state) {
			const el = byId('downtime-' + state);
			if (el) el.classList.toggle('d-none', state !== name);
		});
		const badge = byId('downtime-count-badge');
		if (badge && name !== 'table-container') badge.classList.add('d-none');
	}

	async function hasAnyUptimeData(since) {
		try {
			const resp = await window.apiFetch(
				'/dashboard/uptime?since=' + encodeURIComponent(since) + '&limit=10'
			);
			if (!resp.ok) return false;
			const data = await resp.json();
			return (data.targets || []).some(function (t) {
				return (t.points || []).length > 0;
			});
		} catch (err) {
			console.warn('Uptime fallback check failed:', err);
			return false;
		}
	}

	function renderRows(targets) {
		const tableBody = byId('downtime-table-body');
		if (!tableBody) return;
		tableBody.innerHTML = '';
		targets.forEach(function (target) {
			const services = target.services || {};
			const tr = document.createElement('tr');

			// Built with the DOM API (prevents XSS via href injection)
			const tdName = document.createElement('td');
			const a = document.createElement('a');
			a.href = '/ui/targets/' + encodeURIComponent(target.target_id);
			a.className = 'text-decoration-none text-reset fw-semibold';
			const icon = document.createElement('span');
			icon.className = 'material-icons text-danger me-1';
			icon.textContent = 'error';
			a.appendChild(icon);
			a.appendChild(document.createTextNode(target.name));
			tdName.appendChild(a);
			tr.appendChild(tdName);

			['http_up', 'ping_up', 'tcp_up'].forEach(function (key) {
				const seconds = services[key] ? services[key].downtime_seconds : 0;
				tr.appendChild(createServiceCell({ totalDowntimeMs: seconds * 1000 }));
			});
			tableBody.appendChild(tr);
		});
	}

	async function loadDowntimeOverview() {
		if (downtimeLoading) return;
		downtimeLoading = true;

		const since = new Date(Date.now() - LOOKBACK_WINDOW_MS).toISOString();

		try {
			// The server aggregates the incidents, so the result does not depend on the raw point count.
			const resp = await window.apiFetch('/dashboard/downtime?since=' + encodeURIComponent(since));
			if (!resp.ok) throw new Error('HTTP ' + resp.status);
			const data = await resp.json();
			const targets = data.targets || [];

			if (targets.length === 0) {
				showState((await hasAnyUptimeData(since)) ? 'success' : 'empty');
				return;
			}

			renderRows(targets.slice().sort(function (a, b) {
				return worstSeconds(b) - worstSeconds(a);
			}));
			const badge = byId('downtime-count-badge');
			if (badge) badge.textContent = targets.length + ' affected';
			showState('table-container');
			if (badge) badge.classList.remove('d-none');
		} catch (e) {
			console.error('Failed to load downtime overview:', e);
			showState('error');
		} finally {
			downtimeLoading = false;
		}
	}

	function worstSeconds(target) {
		return Math.max.apply(null, Object.values(target.services || {}).map(function (s) {
			return s.downtime_seconds;
		}).concat(0));
	}

	// ─────────────────────────────────────────────────────────────
	// UI Bindings
	// ─────────────────────────────────────────────────────────────

	function bindRefreshButton() {
		const btn = document.getElementById('dashboard-refresh-btn');
		if (!btn || btn.dataset.bound) return;
		btn.dataset.bound = '1';
		btn.addEventListener('click', function (e) {
			e.preventDefault();
			location.reload();
		});
	}

	document.body.addEventListener('htmx:afterSwap', function (evt) {
		const el = evt && evt.detail && evt.detail.elt;
		if (el && el.classList) {
			el.classList.remove('fade-in');
			// Force reflow to restart animation
			void el.offsetWidth;
			el.classList.add('fade-in');
		}
	});

	document.addEventListener('DOMContentLoaded', function () {
		bindRefreshButton();
		loadDowntimeOverview();

		// Push-driven while the live channel is healthy; the interval only runs as fallback.
		const live = window.JustUpLive && window.JustUpLive.client;
		const unsubscribeLive = live
			? live.subscribe(['alert.changed', 'target.changed'], loadDowntimeOverview, {
				resync: loadDowntimeOverview,
				coalesceMs: 2000,
			})
			: null;

		// Poll only when tab is visible (save resources in background tabs)
		const intervalId = window.setInterval(function () {
			if (document.visibilityState === 'hidden') return;
			if (live && live.state() === 'connected') return;
			loadDowntimeOverview();
		}, POLL_INTERVAL_MS);

		// Cleanup interval when dashboard elements are removed
		document.body.addEventListener('htmx:beforeSwap', function cleanup(evt) {
			const swapTarget = evt.detail.target;
			const dashboard = document.getElementById('downtime-table-container');
			// Only cleanup if dashboard exists and is being removed
			if (dashboard && swapTarget && (swapTarget.contains(dashboard) || swapTarget === document.body)) {
				clearInterval(intervalId);
				if (unsubscribeLive) unsubscribeLive();
				document.body.removeEventListener('htmx:beforeSwap', cleanup);
			}
		});
	});
})();
