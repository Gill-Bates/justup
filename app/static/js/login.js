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
	 * Start lockout countdown timer (memory leak fix - clears old timer first).
	 * @param {number} seconds - Lockout duration in seconds
	 * @param {HTMLElement} errBox - Error message container
	 * @param {HTMLElement} submitBtn - Submit button element
	 */
	function startCountdown(seconds, errBox, submitBtn) {
		// Cleanup: prevent multiple timers running in parallel
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
	 * Initialize login form handler.
	 */
	function init() {
		const form = document.getElementById('login-form');
		if (!form) return;

		const submitBtn = form.querySelector('button[type="submit"]');
		const errBox = document.getElementById('error-msg');

		// Null guard: validate required DOM elements
		if (!submitBtn || !errBox) {
			console.error('Login form: required DOM elements missing');
			return;
		}

		// Capture original button HTML once (prevents race conditions with countdown)
		originalBtnHtml = submitBtn.innerHTML;

		// Add ARIA attributes for screen reader support
		errBox.setAttribute('role', 'alert');
		errBox.setAttribute('aria-live', 'assertive');

		// Cleanup timers on page navigation/HTMX swaps
		window.addEventListener('pagehide', cleanup);
		document.body.addEventListener('htmx:beforeSwap', cleanup);

		form.addEventListener('submit', async function (e) {
			e.preventDefault();

			const usernameInput = document.getElementById('username');
			const passwordInput = document.getElementById('password');

			// Null guard: validate input elements
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

			// Disable button and show loading state
			submitBtn.disabled = true;
			submitBtn.setAttribute('aria-disabled', 'true');
			submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span> Signing in…';

			// Read CSRF token from cookie (handle values with '=' padding)
			const csrfToken = document.cookie.split('; ').find(function (c) {
				return c.startsWith('csrf_token=');
			});
			const csrfValue = csrfToken ? csrfToken.substring(csrfToken.indexOf('=') + 1) : '';

			try {
				const resp = await fetch('/login', {
					method: 'POST',
					credentials: 'same-origin',
					headers: {
						'Content-Type': 'application/json',
						'X-CSRF-Token': csrfValue
					},
					body: JSON.stringify({
						username: usernameInput.value,
						password: passwordInput.value
					})
				});

				if (!resp.ok) {
					// Check for rate limit (429)
					if (resp.status === 429) {
						const retryAfter = parseInt(resp.headers.get('Retry-After') || '10', 10);
						startCountdown(retryAfter, errBox, submitBtn);
						return;
					}

					// Generic error message (prevent user enumeration)
					throw new Error('Invalid username or password');
				}

				// Parse JSON response with error handling
				let data;
				try {
					data = await resp.json();
				} catch (parseErr) {
					throw new Error('Login failed — unexpected server response');
				}

				if (!data.access_token) {
					throw new Error('Login failed — no access token received');
				}

				// Store token in localStorage only (server sets HttpOnly cookie)
				// This allows client-side Authorization headers while keeping the
				// cookie secure from XSS (server must set: HttpOnly; Secure; SameSite=Strict)
				try {
					localStorage.setItem('token', data.access_token);
				} catch (storageErr) {
					console.warn('Could not persist token to localStorage:', storageErr);
					// Token still works via server-set HttpOnly cookie
				}

				window.location.href = '/';
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

	// Initialize when DOM is ready
	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', init);
	} else {
		init();
	}
})();
