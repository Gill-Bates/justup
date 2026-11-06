//
// app/static/js/alerts-panel.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/** Alerts panel on the notifications page: filter auto-submit, acknowledge and close-all actions. */
(function () {
	'use strict';
	function toastError(message) {
		if (window.justUpToast && window.justUpToast.error) window.justUpToast.error(message);
	}

	async function post(url, failMessage, logLabel) {
		try {
			const resp = await window.apiFetch(url, { method: 'POST' });
			if (resp.ok) {
				window.location.reload();
				return;
			}
			const message = await window.JuApiError.fromResponse(resp, failMessage);
			console.error(logLabel, resp.status, message);
			toastError(message);
		} catch (e) {
			console.error(logLabel, e);
			toastError('Network error: ' + e.message);
		}
	}

	const filters = document.getElementById('alert-filters');
	if (filters) {
		filters.addEventListener('change', function (event) {
			if (event.target.matches('select[name="target_id"], select[name="status"], input[type="date"]')) {
				filters.requestSubmit();
			}
		});
	}

	const table = document.getElementById('alerts-table');
	if (table) {
		table.addEventListener('click', function (event) {
			const button = event.target.closest('button[data-alert-id]');
			if (button) post('/alerts/' + encodeURIComponent(button.dataset.alertId) + '/acknowledge', 'Failed to acknowledge alert', 'Acknowledge failed:');
		});
	}

	const closeAll = document.getElementById('close-all-alerts-btn');
	if (closeAll) {
		closeAll.addEventListener('click', async function () {
			if (closeAll.disabled) return;
			const ok = await window.confirmAction({ title: 'Close all alerts', message: 'All open alerts are closed.', confirmLabel: 'Close all' });
			if (ok) post('/alerts/acknowledge-all', 'Failed to close alerts', 'Close all failed:');
		});
	}
})();
