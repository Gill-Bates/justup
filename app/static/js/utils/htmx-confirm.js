//
// app/static/js/utils/htmx-confirm.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Confirmation of htmx actions through the shared design-contract dialog (utils/confirm-dialog.js),
 * instead of the native confirm() that hx-confirm opens.
 *
 * A button opts in with data attributes (no inline script, so it survives every htmx swap):
 *   data-ju-confirm  data-confirm-title  data-confirm-message  data-confirm-label
 * htmx fires `htmx:confirm` before the request; the request itself (verb, CSRF header, swap) stays
 * htmx's: it is issued once, after the user confirmed. A cancel, Escape or a missing text sends nothing.
 */
(function (root) {
	'use strict';

	// Dialog options from the button's dataset, or null when the button is not flagged or has no message.
	function optionsFor(dataset) {
		if (!dataset || !dataset.confirmMessage) return null;
		return {
			title: dataset.confirmTitle || 'Please confirm',
			message: dataset.confirmMessage,
			confirmLabel: dataset.confirmLabel || 'Confirm'
		};
	}

	root.JuHtmxConfirm = { optionsFor: optionsFor };

	document.addEventListener('htmx:confirm', function (event) {
		const detail = event.detail || {};
		const element = detail.elt;
		if (!element || !element.dataset || element.dataset.juConfirm === undefined) return;
		// Flagged: htmx must not continue on its own, whatever the dialog answers.
		event.preventDefault();
		const options = optionsFor(element.dataset);
		if (!options || typeof root.confirmAction !== 'function') return;
		return root.confirmAction(options).then(function (confirmed) {
			if (confirmed) detail.issueRequest(true);
		});
	});
})(typeof window !== 'undefined' ? window : globalThis);
