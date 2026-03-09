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
	const MIN_DOWNTIME_MS = 60 * 1000;          // 60s — minimum duration to count as incident
	const LOOKBACK_WINDOW_MS = 24 * 60 * 60 * 1000; // 24h — time window for downtime stats
	const POLL_INTERVAL_MS = 30 * 1000;         // 30s — polling interval when tab is visible
	const MAX_POINTS_PER_TARGET = 2000;         // Cap to prevent UI freeze with large datasets

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
			td.className += ' text-muted';
			td.textContent = '—';
		}
		return td;
	}

	// ─────────────────────────────────────────────────────────────
	// Downtime Calculation
	// ─────────────────────────────────────────────────────────────

	function calculateDowntimeStats(points) {
		const withTs = (points || []).map(function (p) {
			const ts = new Date(p.ts).getTime();
			// Skip unparseable timestamps (NaN check)
			if (isNaN(ts)) return null;
			return { ...p, _ts: ts };
		}).filter(Boolean);
		withTs.sort(function (a, b) { return a._ts - b._ts; });

		let totalDowntimeMs = 0;
		let incidentCount = 0;
		let wasDown = false;
		let downStartTime = null;

		for (let i = 0; i < withTs.length; i++) {
			const point = withTs[i];
			const isDown = point.value === 0;

			if (isDown && !wasDown) {
				downStartTime = point._ts;
				wasDown = true;
			} else if (!isDown && wasDown) {
				if (downStartTime !== null) {
					const duration = point._ts - downStartTime;
					// Only count as incident if downtime >= MIN_DOWNTIME_MS
					if (duration >= MIN_DOWNTIME_MS) {
						totalDowntimeMs += duration;
						incidentCount++;
					}
				}
				wasDown = false;
				downStartTime = null;
			}
		}

		// Ongoing downtime (still down at end of data)
		if (wasDown && downStartTime !== null) {
			const ongoingDuration = Date.now() - downStartTime;
			if (ongoingDuration >= MIN_DOWNTIME_MS) {
				totalDowntimeMs += ongoingDuration;
				incidentCount++;
			}
		}

		return { totalDowntimeMs, incidentCount };
	}

	// ─────────────────────────────────────────────────────────────
	// Main Data Loading
	// ─────────────────────────────────────────────────────────────

	let downtimeLoading = false;

	async function loadDowntimeOverview() {
		if (downtimeLoading) return;
		downtimeLoading = true;

		const since = new Date(Date.now() - LOOKBACK_WINDOW_MS).toISOString();

		const tableContainer = document.getElementById('downtime-table-container');
		const tableBody = document.getElementById('downtime-table-body');
		const successState = document.getElementById('downtime-success');
		const emptyState = document.getElementById('downtime-empty');
		const loadingState = document.getElementById('downtime-loading');
		const badge = document.getElementById('downtime-count-badge');

		try {
			const resp = await window.apiFetch(
				'/dashboard/downtime?since=' + encodeURIComponent(since) +
				'&limit=' + MAX_POINTS_PER_TARGET
			);

			if (!resp.ok) {
				if (loadingState) loadingState.classList.add('d-hidden');
				if (emptyState) emptyState.classList.remove('d-hidden');
				return;
			}

			let data;
			try {
				data = await resp.json();
			} catch (parseErr) {
				console.error('Failed to parse downtime response:', parseErr);
				if (loadingState) loadingState.classList.add('d-hidden');
				if (emptyState) emptyState.classList.remove('d-hidden');
				return;
			}
			const targets = data.targets || [];

			if (loadingState) loadingState.classList.add('d-hidden');

			if (targets.length === 0) {
				try {
					const uptimeResp = await window.apiFetch(
						'/dashboard/uptime?since=' + encodeURIComponent(since) + '&limit=10'
					);
					if (uptimeResp.ok) {
						const uptimeData = await uptimeResp.json();
						const hasAnyData = (uptimeData.targets || []).some(function (t) {
							return (t.points || []).length > 0;
						});
						if (hasAnyData) {
							if (successState) successState.classList.remove('d-hidden');
						} else {
							if (emptyState) emptyState.classList.remove('d-hidden');
						}
					} else {
						if (emptyState) emptyState.classList.remove('d-hidden');
					}
				} catch (innerErr) {
					console.warn('Uptime fallback check failed:', innerErr);
					if (emptyState) emptyState.classList.remove('d-hidden');
				}

				if (tableContainer) tableContainer.classList.add('d-hidden');
				if (badge) badge.classList.add('d-hidden');
				return;
			}

			const stats = targets.map(function (target) {
				const services = target.services || {};
				const httpStats = calculateDowntimeStats(
					(services.http_up || []).slice(0, MAX_POINTS_PER_TARGET)
				);
				const pingStats = calculateDowntimeStats(
					(services.ping_up || []).slice(0, MAX_POINTS_PER_TARGET)
				);
				const tcpStats = calculateDowntimeStats(
					(services.tcp_up || []).slice(0, MAX_POINTS_PER_TARGET)
				);

				// Worst single-service downtime (used for sorting)
				const worstDowntimeMs = Math.max(
					httpStats.totalDowntimeMs,
					pingStats.totalDowntimeMs,
					tcpStats.totalDowntimeMs
				);

				return {
					target_id: target.target_id,
					name: target.name,
					http: httpStats,
					ping: pingStats,
					tcp: tcpStats,
					worstDowntimeMs,
				};
			}).sort(function (a, b) {
				return b.worstDowntimeMs - a.worstDowntimeMs;
			});

			if (badge) {
				badge.textContent = stats.length + ' affected';
				badge.classList.remove('d-hidden');
			}

			// Build table rows with DOM API (prevents XSS via href injection)
			if (tableBody) {
				tableBody.innerHTML = '';
				stats.forEach(function (s) {
					const tr = document.createElement('tr');

					// Target name column
					const tdName = document.createElement('td');
					const a = document.createElement('a');
					a.href = '/ui/targets/' + encodeURIComponent(s.target_id);
					a.className = 'text-decoration-none text-reset fw-semibold';

					const icon = document.createElement('span');
					icon.className = 'material-icons text-danger me-1';
					icon.textContent = 'error';
					a.appendChild(icon);
					a.appendChild(document.createTextNode(s.name));
					tdName.appendChild(a);
					tr.appendChild(tdName);

					// Service columns (HTTP, Ping, TCP)
					tr.appendChild(createServiceCell(s.http));
					tr.appendChild(createServiceCell(s.ping));
					tr.appendChild(createServiceCell(s.tcp));

					tableBody.appendChild(tr);
				});
			}

			if (tableContainer) tableContainer.classList.remove('d-hidden');
			if (successState) successState.classList.add('d-hidden');
			if (emptyState) emptyState.classList.add('d-hidden');
		} catch (e) {
			console.error('Failed to load downtime overview:', e);
			if (loadingState) loadingState.classList.add('d-hidden');
			if (emptyState) emptyState.classList.remove('d-hidden');
		} finally {
			downtimeLoading = false;
		}
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

		// Poll only when tab is visible (save resources in background tabs)
		const intervalId = window.setInterval(function () {
			if (document.visibilityState === 'hidden') return;
			loadDowntimeOverview();
		}, POLL_INTERVAL_MS);

		// Cleanup interval when dashboard elements are removed
		document.body.addEventListener('htmx:beforeSwap', function cleanup(evt) {
			const swapTarget = evt.detail.target;
			const dashboard = document.getElementById('downtime-table-container');
			// Only cleanup if dashboard exists and is being removed
			if (dashboard && swapTarget && (swapTarget.contains(dashboard) || swapTarget === document.body)) {
				clearInterval(intervalId);
				document.body.removeEventListener('htmx:beforeSwap', cleanup);
			}
		});
	});
})();
