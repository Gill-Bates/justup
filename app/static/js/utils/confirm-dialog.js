//
// app/static/js/utils/confirm-dialog.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * app/static/js/utils/confirm-dialog.js
 * Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
 *
 * Shared confirmation dialog for destructive actions (replaces native confirm()).
 *
 *   if (!(await window.confirmAction({ title, message, confirmLabel }))) return;
 *
 * Static backdrop (a backdrop click or tap never closes it); Escape and Cancel resolve to false.
 * Focus starts on Cancel and returns to the invoking element.
 */
(function () {
	'use strict';

	var modalEl = null;
	var pending = null;
	var ready = false; // Bootstrap ignores hide() while the show transition runs, so Confirm waits for it

	function build() {
		var wrap = document.createElement('div');
		wrap.innerHTML =
			'<div class="modal fade" id="ju-confirm-modal" tabindex="-1" aria-labelledby="ju-confirm-title"' +
			' aria-describedby="ju-confirm-message" data-bs-backdrop="static" data-bs-keyboard="true">' +
			'<div class="modal-dialog modal-dialog-centered"><div class="modal-content">' +
			'<div class="modal-header"><h2 class="modal-title fs-5" id="ju-confirm-title"></h2>' +
			'<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Cancel"></button></div>' +
			'<div class="modal-body"><p class="mb-0" id="ju-confirm-message"></p></div>' +
			'<div class="modal-footer">' +
			'<button type="button" class="btn btn-secondary" id="ju-confirm-cancel" data-bs-dismiss="modal">Cancel</button>' +
			'<button type="button" class="btn btn-danger" id="ju-confirm-ok"></button>' +
			'</div></div></div></div>';
		var el = wrap.firstChild;
		document.body.appendChild(el);
		el.querySelector('#ju-confirm-ok').addEventListener('click', function () {
			if (!pending || !ready) return;
			pending.confirmed = true;
			this.disabled = true; // no second activation while the dialog closes
			window.bootstrap.Modal.getInstance(el).hide();
		});
		el.addEventListener('shown.bs.modal', function () {
			ready = true;
			el.querySelector('#ju-confirm-cancel').focus(); // safe default
		});
		el.addEventListener('hidden.bs.modal', function () {
			ready = false;
			var current = pending;
			pending = null;
			if (!current) return;
			if (current.opener && document.contains(current.opener) && typeof current.opener.focus === 'function') {
				current.opener.focus();
			}
			current.resolve(current.confirmed);
		});
		return el;
	}

	/**
	 * @param {{title?: string, message: string, confirmLabel?: string}} options
	 * @returns {Promise<boolean>} true only when the user confirmed
	 */
	window.confirmAction = function confirmAction(options) {
		var opts = options || {};
		if (!window.bootstrap || !window.bootstrap.Modal) return Promise.resolve(false); // fail closed
		if (pending) pending.resolve(false);
		modalEl = modalEl || build();
		modalEl.querySelector('#ju-confirm-title').textContent = opts.title || 'Please confirm';
		modalEl.querySelector('#ju-confirm-message').textContent = opts.message || '';
		var ok = modalEl.querySelector('#ju-confirm-ok');
		ok.textContent = opts.confirmLabel || 'Confirm';
		ok.disabled = false;
		ready = false;
		return new Promise(function (resolve) {
			pending = { resolve: resolve, confirmed: false, opener: document.activeElement };
			window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
		});
	};
})();
