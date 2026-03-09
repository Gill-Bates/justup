//
// app/static/js/target-detail/test-alert.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Test-alert trigger for the target-detail page.
 * Depends on:
 *   - apiFetch    → global auth-aware fetch wrapper (from base.html)
 *   - justUpToast → global toast utility (from base.html)
 * @module target-detail/test-alert
 */

/**
 * Send a test alert for the given target.
 * Manages button state (icon ↔ spinner) and shows toast feedback.
 * Includes 15-second cooldown after successful send to prevent flooding.
 *
 * @param {number} targetId - Target ID
 * @param {object} [elements] - Optional DOM element overrides
 * @param {HTMLButtonElement} [elements.btn] - Alert button
 * @param {HTMLElement} [elements.icon] - Icon element
 * @param {HTMLElement} [elements.spinner] - Spinner element
 * @returns {Promise<void>}
 */
async function sendTestAlert(targetId, elements) {
	// Input validation
	if (!targetId || typeof targetId !== 'number' || !Number.isFinite(targetId)) {
		console.error('sendTestAlert: invalid targetId', targetId);
		window.justUpToast?.error?.('Cannot send test alert: invalid target.');
		return;
	}

	const btn = elements?.btn || document.getElementById('test-alert-btn');
	const icon = elements?.icon || document.getElementById('test-alert-icon');
	const spinner = elements?.spinner || document.getElementById('test-alert-spinner');

	if (!btn) return;

	// Prevent double-click
	if (btn.disabled) return;

	if (icon) icon.classList.add('d-none');
	if (spinner) spinner.classList.remove('d-none');
	btn.disabled = true;

	let alertSuccess = false;

	try {
		const response = await apiFetch(
			`/targets/${encodeURIComponent(targetId)}/test-alert`,
			{ method: 'POST' }
		);

		// Check status before parsing JSON
		if (!response.ok) {
			let errorMsg = `Server error: ${response.status}`;
			try {
				const errData = await response.json();
				if (typeof errData.detail === 'string') {
					errorMsg = errData.detail;
				}
			} catch {
				// Non-JSON response (e.g. proxy error page)
				const text = await response.text().catch(() => '');
				if (text) errorMsg += ` – ${text.substring(0, 100)}`;
			}
			throw new Error(errorMsg);
		}

		const data = await response.json();
		window.justUpToast?.success?.(data.message || 'Test alert sent!');
		alertSuccess = true;

		// Cooldown to prevent flooding
		const COOLDOWN_MS = 15000; // 15 seconds
		let remaining = Math.ceil(COOLDOWN_MS / 1000);
		const originalHtml = btn.innerHTML;

		const countdownInterval = setInterval(() => {
			remaining--;
			if (remaining <= 0) {
				clearInterval(countdownInterval);
				btn.innerHTML = originalHtml;
				btn.disabled = false;
			} else {
				btn.innerHTML = `<span class="material-icons me-1">timer</span> ${remaining}s`;
			}
		}, 1000);

	} catch (err) {
		console.error('Test alert failed:', err);
		window.justUpToast?.error?.('Test alert failed: ' + err.message);
	} finally {
		// Only reset button immediately if alert failed (success has cooldown)
		if (!alertSuccess) {
			if (icon) icon.classList.remove('d-none');
			if (spinner) spinner.classList.add('d-none');
			if (btn) btn.disabled = false;
		} else {
			// Success case: spinner stays hidden, icon restored
			if (icon) icon.classList.remove('d-none');
			if (spinner) spinner.classList.add('d-none');
		}
	}
}
