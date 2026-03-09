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
		} catch (err) {
			console.error('Spooler stats error:', err);
		}
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

		try {
			const res = await apiFetch(API.log + '?' + params.toString());
			if (!res.ok) return;
			const data = await res.json();

			_renderLogTable(data.entries);
			_renderPagination(data.total);
			_setText('spooler-log-info', data.total > 0 ? `(${data.total} entries)` : '');
		} catch (err) {
			console.error('Spooler log error:', err);
		}
	}

	function _renderLogTable(entries) {
		const tbody = document.getElementById('spooler-log-body');
		if (!tbody) return;

		if (!entries || entries.length === 0) {
			tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-4">No entries</td></tr>';
			return;
		}

		const rows = entries.map(function (e) {
			const channelIcon = e.channel_type === 'signal'
				? '<span class="material-icons me-1" style="font-size:16px;vertical-align:middle;">chat</span><span class="d-none d-md-inline">Signal</span>'
				: '<span class="material-icons me-1" style="font-size:16px;vertical-align:middle;">email</span><span class="d-none d-md-inline">Email</span>';

			const statusBadge = _statusBadge(e.status);
			const errorCell = e.last_error
				? '<span class="text-danger" title="' + _escAttr(e.last_error) + '">' + _esc(e.last_error.substring(0, 160)) + (e.last_error.length > 160 ? '…' : '') + '</span>'
				: '<span class="text-muted">–</span>';

			const timeStr = _formatTime(e.updated_at);
			const recipientDisplay = _esc(e.recipient_name || e.recipient_address);
			const targetDisplay = e.target_name
				? '<a href="/targets/' + e.target_id + '" class="text-decoration-none">' + _esc(e.target_name) + '</a>'
				: '<span class="text-muted">–</span>';

			return '<tr>'
				+ '<td data-label="Time" class="small text-nowrap">' + timeStr + '</td>'
				+ '<td data-label="Channel" class="small text-nowrap">' + channelIcon + '</td>'
				+ '<td data-label="Target" class="small">' + targetDisplay + '</td>'
				+ '<td data-label="Recipient" class="small">' + recipientDisplay + '</td>'
				+ '<td data-label="Type" class="small">' + _typeBadge(e.notification_type) + '</td>'
				+ '<td data-label="Status">' + statusBadge + '</td>'
				+ '<td data-label="Retries" class="text-center small">' + (e.retry_count || 0) + '</td>'
				+ '<td data-label="Error" class="small">' + errorCell + '</td>'
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
		// Previous
		html += '<li class="page-item' + (_currentPage === 0 ? ' disabled' : '') + '">'
			+ '<a class="page-link" href="#" data-page="' + (_currentPage - 1) + '">&laquo;</a></li>';

		// Page numbers (show max 7)
		const start = Math.max(0, _currentPage - 3);
		const end = Math.min(totalPages, start + 7);
		for (let i = start; i < end; i++) {
			html += '<li class="page-item' + (i === _currentPage ? ' active' : '') + '">'
				+ '<a class="page-link" href="#" data-page="' + i + '">' + (i + 1) + '</a></li>';
		}

		// Next
		html += '<li class="page-item' + (_currentPage >= totalPages - 1 ? ' disabled' : '') + '">'
			+ '<a class="page-link" href="#" data-page="' + (_currentPage + 1) + '">&raquo;</a></li>';
		html += '</ul></nav>';

		container.innerHTML = html;

		// Attach click handlers
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
		if (!confirm('Delete all pending and failed notifications from the queue?')) return;

		const btn = document.getElementById('spooler-flush-btn');
		if (btn) btn.disabled = true;

		try {
			const res = await apiFetch(API.flush, { method: 'DELETE' });
			if (!res.ok) {
				const err = await res.json().catch(function () { return {}; });
				throw new Error(err.detail || 'Flush failed');
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
				throw new Error(err.detail || 'Clear log failed');
			}
			const data = await res.json();
			window.justUpToast?.success?.('Cleared ' + (data.deleted || 0) + ' log entries');
			const modalEl = document.getElementById('spoolerClearLogModal');
			const modal = modalEl ? bootstrap.Modal.getInstance(modalEl) : null;
			if (modal) {
				modal.hide();
			}
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

	function _esc(str) {
		const d = document.createElement('div');
		d.textContent = str;
		return d.innerHTML;
	}

	function _escAttr(str) {
		return str.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
	}

	function _statusBadge(status) {
		const map = {
			pending: 'bg-warning text-dark',
			processing: 'bg-info text-dark',
			sent: 'bg-success',
			failed: 'bg-danger',
		};
		const cls = map[status] || 'bg-secondary';
		return '<span class="badge ' + cls + '">' + _esc(status) + '</span>';
	}

	function _typeBadge(type) {
		const map = {
			alert: '🔔',
			test: '🧪',
			welcome: '👋',
		};
		return (map[type] || '') + ' ' + _esc(type);
	}

	function _formatTime(isoStr) {
		if (!isoStr) return '–';
		try {
			const d = new Date(isoStr.endsWith('Z') ? isoStr : isoStr + 'Z');
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
		// Load data
		loadStats();
		loadLog();

		// Refresh button
		const refreshBtn = document.getElementById('spooler-refresh-btn');
		if (refreshBtn) {
			refreshBtn.addEventListener('click', function () {
				loadStats();
				loadLog();
			});
		}

		// Flush button
		const flushBtn = document.getElementById('spooler-flush-btn');
		if (flushBtn) {
			flushBtn.addEventListener('click', flushQueue);
		}

		// Clear Log button
		const clearLogBtn = document.getElementById('confirm-spooler-clear-log-btn');
		if (clearLogBtn) {
			clearLogBtn.addEventListener('click', clearLog);
		}

		// Filter change
		const statusFilter = document.getElementById('spooler-filter-status');
		const channelFilter = document.getElementById('spooler-filter-channel');
		if (statusFilter) statusFilter.addEventListener('change', function () { _currentPage = 0; loadLog(); });
		if (channelFilter) channelFilter.addEventListener('change', function () { _currentPage = 0; loadLog(); });

		// Auto-refresh every 30s
		_autoRefreshTimer = setInterval(function () {
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

	// Also try immediate init if already in DOM (e.g. direct page load)
	if (document.getElementById('spooler-stat-queue')) {
		init();
	}
})();
