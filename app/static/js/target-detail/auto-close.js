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

const _autoCloseUpdates = new Map();

/**
 * Persist the auto-close toggle; revert the toggle on failure.
 * @param {HTMLInputElement} toggle
 * @param {string|number} targetId
 * @returns {Promise<void>}
 */
async function saveAutoCloseAlert(toggle, targetId) {
  if (toggle.disabled) return;
  const wanted = toggle.checked;
  const key = String(targetId);
  let state = _autoCloseUpdates.get(key);
  if (!state) {
    state = { pending: Promise.resolve(), confirmed: !wanted, version: 0 };
    _autoCloseUpdates.set(key, state);
  }
  const version = ++state.version;
  state.pending = state.pending.then(async () => {
    try {
      const res = await apiFetch(`/targets/${targetId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ auto_close_alert: wanted }),
      });
      if (!res.ok) throw new Error(`Request failed: ${res.status}`);
      state.confirmed = wanted;
    } catch (err) {
      if (state.version === version) toggle.checked = state.confirmed;
      console.error("Failed to update auto_close_alert:", err);
    } finally {
      if (state.version === version) _autoCloseUpdates.delete(key);
    }
  });
  return state.pending;
}
