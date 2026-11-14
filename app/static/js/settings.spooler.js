//
// app/static/js/settings.spooler.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Notification Spooler UI module for Settings → Spooler tab.
 * Loads stats tiles, log table with filtering/pagination, and flush action.
 *
 * Depends on:
 *   - apiFetch    → global auth-aware fetch wrapper (from base.html / app.js)
 *   - justUpToast → global toast utility (from base.html / app.js)
 */

(function () {
	'use strict';

	const API = {
		stats: '/settings/notification-queue/stats',
		log: '/settings/notification-queue/log',
		flush: '/settings/notification-queue/flush',
		clearLog: '/settings/notification-queue/clear-log',
	};

	let _currentPage = 0;
	const PAGE_SIZE = 50;
	let _autoRefreshTimer = null;
	// DOM instance the listeners are bound to (HTMX may replace the fragment)
	let _boundRoot = null;
	// Latest loadLog() request: older responses must not overwrite newer ones
	let _logGeneration = 0;
	let _logAbort = null;

	// ── Stats Tiles ───────────────────────────────────────────────────────

	async function loadStats() {
		try {
			const res = await apiFetch(API.stats);
			if (!res.ok) return;
			const data = await res.json();

			_setText('spooler-stat-queue', data.pending || 0);
			_setText('spooler-stat-failed', data.failed || 0);
			_setText('spooler-stat-sent', data.sent || 0);
			_setText('spooler-stat-processing', data.processing || 0);
			_renderChannels(data.channels || {});
			_renderHealth(data);
		} catch (err) {
			console.error('Spooler stats error:', err);
		}
	}

	// Failed handoffs of the last 24 h (server window) keep the queue from looking healthy.
	function _healthState(data) {
		const failed = Number.isInteger(data.failed_recent) && data.failed_recent >= 0 ? data.failed_recent : (data.failed || 0);
		if (failed > 0) {
			return { tone: 'danger', text: failed + ' handoff failure(s) in the last 24 h' };
		}
		if ((data.cooldowns || []).length > 0 || (data.max_retry_count || 0) > 0) {
			return { tone: 'warning', text: 'SMTP/Signal handoff is being retried' };
		}
		return { tone: 'neutral', text: 'No failed handoffs in the last 24 h' };
	}

	function _renderHealth(data) {
		const state = _healthState(data);
		const dot = document.getElementById('spooler-health-dot');
		if (dot) dot.className = window.JuStatusDot.classes(state.tone);
		_setText('spooler-health-text', state.text);
	}

	function _renderChannels(channels) {
		document.querySelectorAll('#spooler-channel-status [data-channel]').forEach(function (row) {
			const info = channels[row.getAttribute('data-channel')] || {};
			['pending', 'failed'].forEach(function (field) {
				const value = row.querySelector('[data-field="' + field + '"]');
				if (value) value.textContent = String(info[field] || 0);
			});
			let hasTimes = false;
			['next_attempt_at', 'oldest_at', 'cooldown_until'].forEach(function (field) {
				const wrapper = row.querySelector('[data-time-field="' + field + '"]');
				if (!wrapper) return;
				const raw = info[field];
				const value = wrapper.querySelector('[data-field="' + field + '"]');
				wrapper.hidden = !raw;
				hasTimes = hasTimes || !!raw;
				if (value) {
					value.textContent = raw ? _formatTime(raw) : '';
					value.dateTime = raw || '';
				}
			});
			const times = row.querySelector('.spooler-channels__times');
			if (times) times.hidden = !hasTimes;
		});
	}

	// ── Log Table ─────────────────────────────────────────────────────────

	async function loadLog() {
		const statusEl = document.getElementById('spooler-filter-status');
		const channelEl = document.getElementById('spooler-filter-channel');
		if (!statusEl || !channelEl) return;

		const params = new URLSearchParams();
		params.set('limit', String(PAGE_SIZE));
		params.set('offset', String(_currentPage * PAGE_SIZE));

		const statusVal = statusEl.value;
		const channelVal = channelEl.value;
		if (statusVal) params.set('status', statusVal);
		if (channelVal) params.set('channel', channelVal);

		const generation = ++_logGeneration;
		if (_logAbort) _logAbort.abort();
		const abort = new AbortController();
		_logAbort = abort;
		try {
			const res = await apiFetch(API.log + '?' + params.toString(), { signal: abort.signal });
			if (generation !== _logGeneration || !res.ok) return;
			const data = await res.json();
			if (generation !== _logGeneration) return;

			// The data shrank below the requested page; jump to the last valid page (strictly lower, so this runs once).
			if (data.total > 0 && (!data.entries || data.entries.length === 0) && _currentPage > 0) {
				_currentPage = Math.max(0, Math.ceil(data.total / PAGE_SIZE) - 1);
				return loadLog();
			}

			_renderLogTable(data.entries, Boolean(statusVal || channelVal));
			_renderPagination(data.total);
			_setText('spooler-log-info', data.total > 0 ? `(${data.total} entries)` : '');
		} catch (err) {
			if (generation !== _logGeneration || (err && err.name === 'AbortError')) return;
			console.error('Spooler log error:', err);
		}
	}

	function _renderLogTable(entries, filtered) {
		const tbody = document.getElementById('spooler-log-body');
		if (!tbody) return;

		if (!entries || entries.length === 0) {
			tbody.innerHTML = '<tr><td colspan="8" class="spooler-log__state text-body-secondary">'
				+ (filtered ? 'No matching entries' : 'No entries') + '</td></tr>';
			return;
		}

		const rows = entries.map(function (e) {
			const channelIcon = _channelIcon(e.channel_type);

			const statusBadge = _statusBadge(e.status, e.retry_count, e.channel_type);
			const errorCell = e.last_error
				? '<span class="text-danger" title="' + escapeHtml(e.last_error) + '">' + escapeHtml(e.last_error.substring(0, 160)) + (e.last_error.length > 160 ? '…' : '') + '</span>'
				: '<span class="text-body-secondary">–</span>';

			const timeStr = _formatTime(e.updated_at);
			const recipientDisplay = escapeHtml(e.recipient_name || e.recipient_address);
			const targetDisplay = e.target_name
				? '<a href="/ui/targets/' + encodeURIComponent(e.target_id) + '" class="text-decoration-none">' + escapeHtml(e.target_name) + '</a>'
				: '<span class="text-body-secondary">–</span>';

			return '<tr>'
				+ '<td data-label="Time" class="small text-nowrap">' + timeStr + '</td>'
				+ '<td data-label="Channel" class="small text-center ju-cell-icon">' + channelIcon + '</td>'
				+ '<td data-label="Target" class="small ju-cell-text">' + targetDisplay + '</td>'
				+ '<td data-label="Recipient" class="small ju-cell-text">' + recipientDisplay + '</td>'
				+ '<td data-label="Type" class="small">' + _typeBadge(e.notification_type) + '</td>'
				+ '<td data-label="Status">' + statusBadge + '</td>'
				+ '<td data-label="Retries" class="text-center small">' + (e.retry_count || 0) + '</td>'
				+ '<td data-label="Error" class="small ju-cell-text">' + errorCell + '</td>'
				+ '</tr>';
		});

		tbody.innerHTML = rows.join('');
	}

	function _renderPagination(total) {
		const container = document.getElementById('spooler-pagination');
		if (!container) return;

		const totalPages = Math.ceil(total / PAGE_SIZE);
		if (totalPages <= 1) {
			container.innerHTML = '';
			return;
		}

		let html = '<nav><ul class="pagination pagination-sm mb-0">';
		html += '<li class="page-item' + (_currentPage === 0 ? ' disabled' : '') + '">'
			+ '<a class="page-link" href="#" data-page="' + (_currentPage - 1) + '">&laquo;</a></li>';

		// Page numbers (show max 7)
		const start = Math.max(0, _currentPage - 3);
		const end = Math.min(totalPages, start + 7);
		for (let i = start; i < end; i++) {
			html += '<li class="page-item' + (i === _currentPage ? ' active' : '') + '">'
				+ '<a class="page-link" href="#" data-page="' + i + '">' + (i + 1) + '</a></li>';
		}

		html += '<li class="page-item' + (_currentPage >= totalPages - 1 ? ' disabled' : '') + '">'
			+ '<a class="page-link" href="#" data-page="' + (_currentPage + 1) + '">&raquo;</a></li>';
		html += '</ul></nav>';

		container.innerHTML = html;

		container.querySelectorAll('[data-page]').forEach(function (link) {
			link.addEventListener('click', function (ev) {
				ev.preventDefault();
				const page = parseInt(this.getAttribute('data-page'), 10);
				if (page >= 0 && page < totalPages) {
					_currentPage = page;
					loadLog();
				}
			});
		});
	}

	// ── Flush ─────────────────────────────────────────────────────────────

	async function flushQueue() {
		if (!(await window.confirmAction({ title: 'Delete queued notifications', message: 'All pending and failed notifications are deleted from the queue. This cannot be undone.', confirmLabel: 'Delete all' }))) return;

		const btn = document.getElementById('spooler-flush-btn');
		if (btn) btn.disabled = true;

		try {
			const res = await apiFetch(API.flush, { method: 'DELETE' });
			if (!res.ok) {
				const err = await res.json().catch(function () { return {}; });
				throw new Error(window.JuApiError.message(err.detail, 'Flush failed'));
			}
			const data = await res.json();
			window.justUpToast?.success?.('Flushed ' + (data.deleted || 0) + ' notifications');
			_currentPage = 0;
			loadStats();
			loadLog();
		} catch (err) {
			console.error('Flush error:', err);
			window.justUpToast?.error?.('Flush failed: ' + err.message);
		} finally {
			if (btn) btn.disabled = false;
		}
	}

	// ── Clear Log ──────────────────────────────────────────────────────────

	async function clearLog() {
		const btn = document.getElementById('confirm-spooler-clear-log-btn');
		if (btn) btn.disabled = true;

		try {
			const res = await apiFetch(API.clearLog, { method: 'DELETE' });
			if (!res.ok) {
				const err = await res.json().catch(function () { return {}; });
				throw new Error(window.JuApiError.message(err.detail, 'Clear log failed'));
			}
			const data = await res.json();
			window.justUpToast?.success?.('Cleared ' + (data.deleted || 0) + ' log entries');
			bootstrap.Modal.getInstance(document.getElementById('spoolerClearLogModal'))?.hide();
			_currentPage = 0;
			loadStats();
			loadLog();
		} catch (err) {
			console.error('Clear log error:', err);
			window.justUpToast?.error?.('Clear log failed: ' + err.message);
		} finally {
			if (btn) btn.disabled = false;
		}
	}

	// ── Helpers ───────────────────────────────────────────────────────────

	function _setText(id, text) {
		const el = document.getElementById(id);
		if (el) el.textContent = String(text);
	}

	// Channel is shown as an icon only; the name stays available as tooltip and screen-reader text.
	function _channelIcon(channelType) {
		const channel = channelType === 'signal' ? ['chat', 'Signal'] : ['email', 'Email'];
		return '<span title="' + channel[1] + '">'
			+ '<span class="material-icons" style="font-size:16px;vertical-align:middle;" aria-hidden="true">' + channel[0] + '</span>'
			+ '<span class="visually-hidden">' + channel[1] + '</span></span>';
	}

	// "sent" only means the server accepted the message; the caveat is documented, not shown as tooltip.
	function _statusBadge(status, retries, channelType) {
		const handoff = channelType === 'signal' ? 'Signal handoff' : 'SMTP handoff';
		const map = {
			pending: ['warning', 'Waiting for handoff', 'Waiting for ' + handoff],
			processing: ['info', 'Handing over', 'Being handed to the server'],
			sent: ['success', 'Sent', ''],
			failed: ['danger', 'Handoff failed', 'Handoff to the server failed'],
		};
		const entry = map[status];
		if (!entry) return JuStatusDot.html('neutral', status);
		const retrying = status === 'pending' && retries > 0;
		const label = retrying ? 'Retrying handoff' : entry[1];
		const title = retrying ? handoff + ' is being retried' : entry[2];
		return JuStatusDot.html(entry[0], label, { title });
	}

	function _typeBadge(type) {
		const map = {
			alert: '🔔',
			test: '🧪',
			welcome: '👋',
		};
		return (map[type] || '') + ' ' + escapeHtml(type);
	}

	function _formatTime(isoStr) {
		if (!isoStr) return '–';
		try {
			const d = new Date(parseUtcTimestamp(isoStr));
			if (isNaN(d.getTime())) return isoStr.substring(0, 19);
			return d.toLocaleString(undefined, {
				month: 'short', day: 'numeric',
				hour: '2-digit', minute: '2-digit', second: '2-digit',
			});
		} catch (_e) {
			return isoStr.substring(0, 19);
		}
	}

	// ── Init ──────────────────────────────────────────────────────────────

	function init() {
		const root = document.getElementById('spooler-stat-queue');
		// Same DOM instance: listeners are already bound, only refresh the data
		if (root && root === _boundRoot) {
			loadStats();
			loadLog();
			return;
		}
		_boundRoot = root;
		if (_autoRefreshTimer !== null) {
			clearInterval(_autoRefreshTimer);
			_autoRefreshTimer = null;
		}
		loadStats();
		loadLog();

		const refreshBtn = document.getElementById('spooler-refresh-btn');
		if (refreshBtn) {
			refreshBtn.addEventListener('click', function () {
				loadStats();
				loadLog();
			});
		}

		const flushBtn = document.getElementById('spooler-flush-btn');
		if (flushBtn) {
			flushBtn.addEventListener('click', flushQueue);
		}

		const clearLogBtn = document.getElementById('confirm-spooler-clear-log-btn');
		if (clearLogBtn) {
			clearLogBtn.addEventListener('click', clearLog);
		}

		const statusFilter = document.getElementById('spooler-filter-status');
		const channelFilter = document.getElementById('spooler-filter-channel');
		if (statusFilter) statusFilter.addEventListener('change', function () { _currentPage = 0; loadLog(); });
		if (channelFilter) channelFilter.addEventListener('change', function () { _currentPage = 0; loadLog(); });

		// Auto-refresh every 30s
		_autoRefreshTimer = setInterval(function () {
			// Stop when the bound fragment was replaced or removed
			if (!_boundRoot || !_boundRoot.isConnected) {
				clearInterval(_autoRefreshTimer);
				_autoRefreshTimer = null;
				_boundRoot = null;
				return;
			}
			// Only refresh if the spooler tab is visible
			const pane = document.getElementById('spooler-pane');
			if (pane && pane.classList.contains('active')) {
				loadStats();
			}
		}, 30000);

		// Expose for external refresh
		window._refreshSpoolerStats = function () { loadStats(); loadLog(); };
	}

	// Entry point: run init when spooler pane is revealed via HTMX
	// The HTMX after-settle event calls window._initSpoolerTab()
	window._initSpoolerTab = init;
	// Test hook: pure state helpers
	window._spoolerInternals = { healthState: _healthState, statusBadge: _statusBadge, channelIcon: _channelIcon };

	// Also try immediate init if already in DOM (e.g. direct page load)
	if (document.getElementById('spooler-stat-queue')) {
		init();
	}
})();
