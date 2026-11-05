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
	const groups = {
		service: { box: root.querySelector('[data-service-group="service"]'), list: root.querySelector('[data-service-list="service"]') },
		version: { box: root.querySelector('[data-service-group="version"]'), list: root.querySelector('[data-service-list="version"]') },
	};
	let token = null;
	let count = 0;
	let busy = false;
	let confirming = false;
	function setBusy(value) {
		busy = value;
		previewButton.disabled = value;
		confirmButton.disabled = value;
		root.setAttribute('aria-busy', String(value));
	}
	const describe = (name, product) => [name, product].filter(Boolean).join(' / ') || 'unknown';
	function message(change) {
		const endpoint = `${change.protocol.toUpperCase()}:${change.port}`;
		if (change.kind === 'version_changed') {
			return `${endpoint} ${describe(change.name, change.product)} ${change.previous_version || '-'} → ${change.version || '-'}`;
		}
		return `${endpoint} ${describe(change.previous_name, change.previous_product)} → ${describe(change.name, change.product)}`;
	}
	function clear() {
		token = null;
		count = 0;
		confirmButton.hidden = true;
		for (const group of Object.values(groups)) {
			group.list.replaceChildren();
			group.box.hidden = true;
		}
		error.hidden = true;
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
		if (busy || confirming) return;
		clear();
		status.textContent = 'Detecting…';
		setBusy(true);
		try {
			const preview = await post(`/network-scans/services/${targetId}/preview`, {});
			for (const change of preview.changes) {
				const group = groups[change.kind === 'version_changed' ? 'version' : 'service'];
				const item = document.createElement('li');
				item.textContent = message(change);
				group.list.append(item);
				group.box.hidden = false;
			}
			count = preview.changes.length;
			token = count ? preview.token : null;
			confirmButton.hidden = !token;
			status.textContent = count ? `${count} ${count === 1 ? 'change' : 'changes'}` : 'No changes';
		} catch (failure) {
			error.textContent = failure.message;
			error.hidden = false;
			status.textContent = '';
		} finally {
			setBusy(false);
		}
	});
	confirmButton.addEventListener('click', async () => {
		if (busy || confirming || !token) return;
		confirming = true;
		let accepted = false;
		try {
			accepted = typeof window.confirmAction === 'function' && await window.confirmAction({
				title: 'Apply service changes',
				message: `${count} stored ${count === 1 ? 'entry is' : 'entries are'} updated.`,
				confirmLabel: 'Apply',
			});
		} finally {
			confirming = false;
		}
		if (!accepted || !token) return;
		const pendingToken = token;
		token = null;
		setBusy(true);
		try {
			await post(`/network-scans/services/${targetId}/confirm`, { token: pendingToken });
			status.textContent = 'Applied';
			confirmButton.hidden = true;
			error.hidden = true;
			window.location.reload();
		} catch (failure) {
			confirmButton.hidden = true;
			error.textContent = failure.message;
			error.hidden = false;
			status.textContent = '';
		} finally {
			setBusy(false);
		}
	});
})();
