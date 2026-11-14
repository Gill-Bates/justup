//
// app/static/js/base-init.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Page-wide behaviour that used to be inline in base.html: the onboarding pulse on the
// Targets nav item and the HTMX 5xx retry guard.

'use strict';

// Onboarding: pulse the Targets nav item while no targets exist.
(function () {
	const navTargets = document.getElementById('nav-targets');
	if (!navTargets || navTargets.classList.contains('active')) return;

	// Use apiFetch for centralized auth/error handling
	if (typeof window.apiFetch === 'function') {
		window.apiFetch('/targets')
			.then(r => r.ok ? r.json() : null)
			.then(data => {
				if (data && Array.isArray(data) && data.length === 0) {
					navTargets.classList.add('nav-link-pulse');
				}
			})
			.catch(() => { });
	}
})();

// HTMX: stop polling after repeated 5xx errors to prevent retry storms.
(function () {
	document.body.addEventListener('htmx:responseError', function (evt) {
		const elt = evt.detail.elt;
		const xhr = evt.detail.xhr;

		// Only handle 5xx errors (server issues) - let 4xx through (auth, validation)
		if (xhr.status < 500) return;

		const retryCount = parseInt(elt.dataset.htmxRetryCount || '0');
		const MAX_RETRIES = 3;

		if (retryCount >= MAX_RETRIES) {
			// Stop polling after 3 consecutive failures
			const originalTrigger = elt.getAttribute('hx-trigger');
			if (originalTrigger) {
				// Save original trigger so it can be restored on page reload
				elt.dataset.htmxOriginalTrigger = originalTrigger;
				elt.removeAttribute('hx-trigger');
				console.warn('HTMX polling stopped after', MAX_RETRIES, 'failures:',
					evt.detail.pathInfo.requestPath);
			}
			return;
		}

		elt.dataset.htmxRetryCount = (retryCount + 1).toString();
	});

	document.body.addEventListener('htmx:afterRequest', function (evt) {
		if (evt.detail.successful) {
			const elt = evt.detail.elt;
			if (elt.dataset.htmxRetryCount) {
				delete elt.dataset.htmxRetryCount;
			}
			// Restore trigger if it was disabled by retry limit
			if (elt.dataset.htmxOriginalTrigger && !elt.getAttribute('hx-trigger')) {
				elt.setAttribute('hx-trigger', elt.dataset.htmxOriginalTrigger);
				delete elt.dataset.htmxOriginalTrigger;
				if (typeof htmx !== 'undefined' && htmx.process) {
					htmx.process(elt);
				}
			}
		}
	});
})();
