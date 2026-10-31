//
// app/static/js/change-password.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Mandatory password change form (bootstrap admin or admin-reset login).
 * Authenticates through the session cookie and the CSRF double-submit header.
 */
(function () {
	'use strict';

	function showError(box, message) {
		box.textContent = message;
		box.classList.remove('d-none');
	}

	function init() {
		const form = document.getElementById('change-password-form');
		const errBox = document.getElementById('error-msg');
		const newInput = document.getElementById('new-password');
		const confirmInput = document.getElementById('confirm-password');
		if (!form || !errBox || !newInput || !confirmInput) {
			console.error('Change password form: required DOM elements missing');
			return;
		}
		const submitBtn = form.querySelector('button[type="submit"]');
		const originalBtnHtml = submitBtn.innerHTML;

		form.addEventListener('submit', async function (e) {
			e.preventDefault();
			errBox.classList.add('d-none');
			if (submitBtn.disabled) return;

			if (newInput.value.length < 8) {
				showError(errBox, 'The password must be at least 8 characters long.');
				return;
			}
			if (newInput.value !== confirmInput.value) {
				showError(errBox, 'The passwords do not match.');
				return;
			}

			submitBtn.disabled = true;
			submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span> Saving…';

			try {
				const resp = await fetch('/me/complete-required-change', {
					method: 'POST',
					credentials: 'same-origin',
					headers: {
						'Content-Type': 'application/json',
						'X-CSRF-Token': getCsrfToken()
					},
					body: JSON.stringify({ new_password: newInput.value })
				});
				if (!resp.ok) {
					let detail = 'Password change failed';
					try {
						const body = await resp.json();
						if (typeof body.detail === 'string') detail = body.detail;
					} catch (parseErr) {
						// Keep the generic message
					}
					throw new Error(detail);
				}
				// The server revoked all sessions; sign in again with the new password.
				try {
					localStorage.removeItem('token');
				} catch (storageErr) {
					// localStorage may be blocked; nothing to clean up then
				}
				window.location.href = '/ui/login';
			} catch (err) {
				showError(errBox, err.message);
				submitBtn.disabled = false;
				submitBtn.innerHTML = originalBtnHtml;
			}
		});
	}

	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', init);
	} else {
		init();
	}
})();
