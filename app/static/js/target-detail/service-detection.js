//
// app/static/js/target-detail/service-detection.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

(() => {
	'use strict';
	const root = document.getElementById('service-detection');
	if (!root || typeof window.apiFetch !== 'function') return;

	const targetId = Number(root.dataset.targetId);
	const previewButton = root.querySelector('[data-service-action="preview"]');
	const confirmButton = root.querySelector('[data-service-action="confirm"]');
	const status = root.querySelector('[data-service-status]');
	const error = root.querySelector('[data-service-error]');
	const changes = root.querySelector('[data-service-changes]');
	let token = null;
	let busy = false;

	function setBusy(value) {
		busy = value;
		previewButton.disabled = value;
		confirmButton.disabled = value;
		root.setAttribute('aria-busy', String(value));
	}

	function message(change) {
		const oldService = [change.previous_name, change.previous_product].filter(Boolean).join(' / ') || 'unknown';
		const newService = [change.name, change.product].filter(Boolean).join(' / ') || 'unknown';
		const endpoint = `${change.protocol.toUpperCase()}:${change.port}`;
		if (change.kind === 'version_changed') {
			return `${endpoint}: version ${change.previous_version || 'unknown'} → ${change.version || 'unknown'}`;
		}
		if (change.kind === 'identified') return `${endpoint}: identified ${newService}`;
		return `${endpoint}: service ${oldService} → ${newService}`;
	}

	async function post(path, body) {
		const response = await window.apiFetch(path, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body),
		});
		const result = await response.json().catch(() => ({}));
		if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : `Request failed (${response.status})`);
		return result;
	}

	previewButton.addEventListener('click', async () => {
		if (busy) return;
		token = null;
		confirmButton.hidden = true;
		changes.replaceChildren();
		changes.hidden = true;
		error.hidden = true;
		status.textContent = 'Detecting services…';
		setBusy(true);
		try {
			const preview = await post(`/network-scans/services/${targetId}/preview`, {});
			token = preview.token;
			for (const change of preview.changes) {
				const item = document.createElement('li');
				item.textContent = message(change);
				changes.append(item);
			}
			changes.hidden = preview.changes.length === 0;
			confirmButton.hidden = !token;
			status.textContent = token ? 'Review the detected changes before applying them.' : 'No service changes found.';
		} catch (failure) {
			error.textContent = failure.message;
			error.hidden = false;
			status.textContent = '';
		} finally {
			setBusy(false);
		}
	});

	confirmButton.addEventListener('click', async () => {
		if (busy || !token) return;
		const pendingToken = token;
		token = null;
		setBusy(true);
		try {
			await post(`/network-scans/services/${targetId}/confirm`, { token: pendingToken });
			status.textContent = 'Detected services applied.';
			confirmButton.hidden = true;
			error.hidden = true;
			window.location.reload();
		} catch (failure) {
			confirmButton.hidden = true;
			error.textContent = failure.message;
			error.hidden = false;
			status.textContent = 'Detect services again to review current values.';
		} finally {
			setBusy(false);
		}
	});
})();
