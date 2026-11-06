//
// app/static/js/login.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Login page form handler with rate limiting, CSRF protection, and lockout countdown.
 */
(function () {
	'use strict';

	let lockoutTimer = null;
	let originalBtnHtml = null;  // Captured once at init to prevent race conditions

	/**
	 * Cleanup countdown timer and event listeners
	 */
	function cleanup() {
		if (lockoutTimer) {
			clearInterval(lockoutTimer);
			lockoutTimer = null;
		}
	}

	/**
	 * Start the lockout countdown; a running timer is cleared first.
	 * @param {number} seconds - Lockout duration in seconds
	 * @param {HTMLElement} errBox - Error message container
	 * @param {HTMLElement} submitBtn - Submit button element
	 */
	function startCountdown(seconds, errBox, submitBtn) {
		cleanup();

		let remaining = seconds;

		submitBtn.disabled = true;
		submitBtn.setAttribute('aria-disabled', 'true');

		function update() {
			remaining--;
			if (remaining <= 0) {
				clearInterval(lockoutTimer);
				lockoutTimer = null;
				errBox.classList.add('d-none');
				submitBtn.disabled = false;
				submitBtn.removeAttribute('aria-disabled');
				submitBtn.innerHTML = originalBtnHtml;
				return;
			}
			errBox.textContent = `Incorrect credentials. Please try again in ${remaining} second${remaining !== 1 ? 's' : ''}.`;
			errBox.classList.remove('d-none');
		}

		// Display initial countdown (before first interval tick)
		errBox.textContent = `Incorrect credentials. Please try again in ${remaining} second${remaining !== 1 ? 's' : ''}.`;
		errBox.classList.remove('d-none');
		lockoutTimer = setInterval(update, 1000);
	}

	/**
	 * The page the user asked for before the sign-in (`?next=`), or '' when absent or unsafe.
	 * Only same-origin relative paths pass; the server redirect never emits anything else.
	 * @returns {string}
	 */
	function requestedPage() {
		const next = new URLSearchParams(window.location.search).get('next') || '';
		if (next.length > 2048 || !/^\/(?![/\\])/.test(next)) return '';
		// Control characters and backslashes are treated like '/' by some browsers.
		if (next.includes('\\') || Array.from(next).some((ch) => ch.charCodeAt(0) < 0x20 || ch.charCodeAt(0) === 0x7f)) return '';
		if (next.startsWith('/ui/login')) return '';
		return next;
	}

	function init() {
		const form = document.getElementById('login-form');
		if (!form) return;

		const submitBtn = form.querySelector('button[type="submit"]');
		const errBox = document.getElementById('error-msg');

		if (!submitBtn || !errBox) {
			console.error('Login form: required DOM elements missing');
			return;
		}

		// Capture original button HTML once (prevents race conditions with countdown)
		originalBtnHtml = submitBtn.innerHTML;

		errBox.setAttribute('role', 'alert');
		errBox.setAttribute('aria-live', 'assertive');

		// Cleanup timers on page navigation/HTMX swaps
		window.addEventListener('pagehide', cleanup);
		document.body.addEventListener('htmx:beforeSwap', cleanup);

		form.addEventListener('submit', async function (e) {
			e.preventDefault();

			const usernameInput = document.getElementById('username');
			const passwordInput = document.getElementById('password');

			if (!usernameInput || !passwordInput) {
				console.error('Login form: username or password input missing');
				return;
			}

			errBox.classList.add('d-none');

			if (lockoutTimer) {
				return; // Still locked out
			}

			// Prevent double-submit during API call
			if (submitBtn.disabled) {
				return; // Already submitting
			}

			submitBtn.disabled = true;
			submitBtn.setAttribute('aria-disabled', 'true');
			submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span> Signing in…';


			try {
				const resp = await fetch('/login', withCsrf({
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({
						username: usernameInput.value,
						password: passwordInput.value,
						website: (document.getElementById('website') || {}).value || ''
					})
				}));

				if (!resp.ok) {
					if (resp.status === 429) {
						const retryAfter = parseInt(resp.headers.get('Retry-After') || '10', 10);
						startCountdown(retryAfter, errBox, submitBtn);
						return;
					}

					// 403 is the CSRF check, which a retry with the same page cannot pass.
					if (resp.status === 403) throw new Error('Your session expired. Reload the page and sign in again.');
					if (resp.status >= 500) throw new Error('Sign-in is unavailable right now. Please try again later.');
					// Generic error message (prevent user enumeration)
					throw new Error('Invalid username or password');
				}

				let data;
				try {
					data = await resp.json();
				} catch (parseErr) {
					throw new Error('Login failed — unexpected server response');
				}

				const next = requestedPage();
				if (data.must_change_password) {
					// The password change signs the user out again; carry the target through it.
					window.location.href = '/ui/change-password' + (next ? '?next=' + encodeURIComponent(next) : '');
				} else {
					window.location.href = next || '/';
				}
			} catch (err) {
				errBox.textContent = err.message;
				errBox.classList.remove('d-none');
			} finally {
				// Re-enable button (unless locked out)
				if (!lockoutTimer) {
					submitBtn.disabled = false;
					submitBtn.removeAttribute('aria-disabled');
					submitBtn.innerHTML = originalBtnHtml;
				}
			}
		});
	}

	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', init);
	} else {
		init();
	}
})();
