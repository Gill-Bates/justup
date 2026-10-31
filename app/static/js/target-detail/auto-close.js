//
// app/static/js/target-detail/auto-close.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
/**
 * Auto-close-alert toggle persistence for the target-detail page.
 * Depends on:
 *   - apiFetch → global auth/CSRF-aware fetch wrapper (from base.html)
 * @module target-detail/auto-close
 */

/**
 * Persist the auto-close toggle; revert the toggle on failure.
 * @param {HTMLInputElement} toggle
 * @param {string|number} targetId
 * @returns {Promise<void>}
 */
async function saveAutoCloseAlert(toggle, targetId) {
	if (toggle.disabled) return;
	const wanted = toggle.checked;
	try {
		const res = await apiFetch(`/targets/${targetId}`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ auto_close_alert: wanted }),
		});
		if (!res.ok) {
			toggle.checked = !wanted;
			console.error('Failed to update auto_close_alert:', res.status);
		}
	} catch (err) {
		toggle.checked = !wanted;
		console.error('Failed to update auto_close_alert:', err);
	}
}
