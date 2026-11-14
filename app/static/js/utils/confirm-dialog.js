//
// app/static/js/utils/confirm-dialog.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Shared confirmation dialog for destructive actions (replaces native confirm()).
 *
 *   if (!(await window.confirmAction({ title, message, confirmLabel }))) return;
 *
 * Static backdrop (a backdrop click or tap never closes it); Escape and Cancel resolve to false.
 * Focus starts on Cancel and returns to the invoker (utils/modal-focus.js).
 *
 * Bootstrap cannot stack modals. When another modal is open, it is hidden first (its form state stays
 * in the DOM), the confirmation is shown alone, and the modal is shown again once the confirmation
 * closed, whatever the answer. The promise resolves after that, so the caller sees the original modal
 * again, e.g. to show an error or to hide it after a successful save.
 */
(function () {
	'use strict';

	var modalEl = null;
	var pending = null;
	var parentEl = null; // modal that was open when the confirmation was requested
	var busy = false; // from the request until its promise resolved (includes the hand-back to the parent)
	var ready = false; // Bootstrap ignores hide() while the show transition runs, so Confirm waits for it

	function build() {
		var wrap = document.createElement('div');
		wrap.innerHTML =
			'<div class="modal fade ju-modal--critical" id="ju-confirm-modal" tabindex="-1" aria-labelledby="ju-confirm-title"' +
			' aria-describedby="ju-confirm-message" data-bs-backdrop="static" data-bs-keyboard="true">' +
			'<div class="modal-dialog modal-dialog-centered modal-dialog-scrollable"><div class="modal-content">' +
			'<div class="modal-header"><h2 class="modal-title ju-modal-title">' +
			'<span class="material-icons ju-modal-icon" aria-hidden="true">warning</span><span id="ju-confirm-title"></span></h2>' +
			'<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Cancel"></button></div>' +
			'<div class="modal-body"><p class="ju-modal-consequence">' +
			'<span class="material-icons ju-icon-inline" aria-hidden="true">warning</span>' +
			'<span id="ju-confirm-message"></span></p></div>' +
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
			ready = true; // initial focus (Cancel) is set by utils/modal-focus.js
		});
		el.addEventListener('hidden.bs.modal', function () {
			ready = false;
			var current = pending;
			pending = null;
			var parent = parentEl;
			parentEl = null;
			if (!parent) {
				if (current) current.resolve(current.confirmed);
				return;
			}
			if (window.JuModalFocus) window.JuModalFocus.handBack(el);
			parent.addEventListener('shown.bs.modal', function () {
				if (current) current.resolve(current.confirmed);
			}, { once: true });
			window.bootstrap.Modal.getOrCreateInstance(parent).show();
		});
		return el;
	}

	// Hide the parent, then show the confirmation once the parent is fully hidden (one backdrop, one scroll lock).
	function handOver(parent) {
		var modal = window.bootstrap.Modal.getOrCreateInstance(parent);
		var started = false;
		parent.addEventListener('hide.bs.modal', function () { started = true; }, { once: true });
		if (window.JuModalFocus) window.JuModalFocus.hold(parent);
		parent.addEventListener('hidden.bs.modal', function () {
			window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
		}, { once: true });
		modal.hide();
		if (started) return;
		// Bootstrap ignores hide() during the parent's own show transition: retry once it has finished.
		parent.addEventListener('shown.bs.modal', function () { modal.hide(); }, { once: true });
	}
	/**
	 * @param {{title?: string, message: string, confirmLabel?: string}} options
	 * @returns {Promise<boolean>} true only when the user confirmed
	 */
	window.confirmAction = function confirmAction(options) {
		var opts = options || {};
		if (!window.bootstrap || !window.bootstrap.Modal) return Promise.resolve(false); // fail closed
		if (busy) return Promise.resolve(false); // one confirmation at a time; the active one stays untouched
		modalEl = modalEl || build();
		modalEl.querySelector('#ju-confirm-title').textContent = opts.title || 'Please confirm';
		modalEl.querySelector('#ju-confirm-message').textContent = opts.message || '';
		var ok = modalEl.querySelector('#ju-confirm-ok');
		ok.textContent = opts.confirmLabel || 'Confirm';
		ok.disabled = false;
		ready = false;
		var parent = document.querySelector('.modal.show:not(#ju-confirm-modal)');
		busy = true;
		return new Promise(function (resolve) {
			pending = { confirmed: false, resolve: function (value) { busy = false; resolve(value); } };
			parentEl = parent;
			if (parent) handOver(parent);
			else window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
		});
	};

	/**
	 * Promise wrapper for a dedicated confirmation modal that is already in the page (for example the
	 * Settings dialogs that need their own button style). Resolves true only on an explicit confirm.
	 * @param {{modalId: string, confirmId: string, nameId: string, name: string}} spec
	 * @returns {Promise<boolean>}
	 */
	window.confirmViaModal = function confirmViaModal(spec) {
		var el = document.getElementById(spec.modalId);
		var confirmBtn = document.getElementById(spec.confirmId);
		var nameEl = document.getElementById(spec.nameId);
		if (!el || !confirmBtn || !nameEl || !window.bootstrap || !window.bootstrap.Modal) return Promise.resolve(false);
		if (el.dataset && el.dataset.juConfirmBusy === '1') return Promise.resolve(false); // one answer per dialog (DC-MODAL-14)
		if (el.dataset) el.dataset.juConfirmBusy = '1';
		nameEl.textContent = '"' + spec.name + '"';
		var modal = window.bootstrap.Modal.getOrCreateInstance(el);
		return new Promise(function (resolve) {
			var confirmed = false;
			function onConfirm() {
				confirmed = true;
				modal.hide();
			}
			confirmBtn.addEventListener('click', onConfirm, { once: true });
			el.addEventListener('hidden.bs.modal', function () {
				confirmBtn.removeEventListener('click', onConfirm);
				if (el.dataset) delete el.dataset.juConfirmBusy;
				resolve(confirmed);
			}, { once: true });
			modal.show();
		});
	};
})();
