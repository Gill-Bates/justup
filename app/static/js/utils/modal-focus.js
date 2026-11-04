//
// app/static/js/utils/modal-focus.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * app/static/js/utils/modal-focus.js
 * Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
 *
 * One focus lifecycle for every Bootstrap modal (static and dynamically created).
 * The HTML `autofocus` attribute is not used: Bootstrap 5.3 modals are not focused by it.
 *
 *  - show.bs.modal:   remember the invoking element.
 *  - shown.bs.modal:  focus the initial control (see pickInitialFocus).
 *  - hidden.bs.modal: return focus to the invoker if it is still in the DOM, else a fallback.
 *
 * Dialog hand-over (JuModalFocus.hold / confirmAction): Bootstrap cannot stack modals, so a dialog that
 * needs a child dialog is hidden first and shown again afterwards. hold(modal) makes the next hidden
 * event of that modal skip the focus return and keeps its opener; when it is shown again, focus goes
 * back to the control that had it (the trigger of the child dialog) instead of the initial control.
 *
 * Initial focus order:
 *  1. an enabled, visible control marked data-ju-initial-focus (explicit override);
 *  2. destructive/critical dialog (.ju-modal--critical): the first required text-like input
 *     (the user must type anyway, nothing is committed by focusing it), else the safe
 *     Cancel/Close action of the footer;
 *  3. other dialogs: the first enabled, visible, editable text-like field in the body;
 *  4. otherwise the first focusable control of the body, the footer Cancel/Close, the close button;
 *  5. the dialog itself (Bootstrap default, tabindex="-1").
 */
(function (root) {
	'use strict';

	var TEXT_FIELD = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=button])' +
		':not([type=submit]):not([type=file]):not([type=range]):not([type=color]), select, textarea';
	var FOCUSABLE = TEXT_FIELD + ', input[type=checkbox], input[type=radio], button, a[href], [tabindex]:not([tabindex="-1"])';
	var openers = new WeakMap();
	var held = new WeakMap(); // modal -> control to refocus when it is shown again after a hand-over
	var skipReturn = new WeakSet(); // child dialogs whose next hidden event hands focus back to the parent

	function usable(el) {
		if (!el || el.disabled || el.hidden || el.readOnly) return false;
		if (el.closest('[hidden], [inert]') || el.getAttribute('aria-hidden') === 'true') return false;
		return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
	}

	function firstUsable(scope, selector) {
		var list = scope.querySelectorAll(selector);
		for (var i = 0; i < list.length; i++) if (usable(list[i])) return list[i];
		return null;
	}

	function pickInitialFocus(modal) {
		var body = modal.querySelector('.modal-body') || modal;
		var footer = modal.querySelector('.modal-footer') || modal;
		var explicit = firstUsable(modal, '[data-ju-initial-focus]');
		if (explicit) return explicit;
		var safe = firstUsable(footer, '[data-bs-dismiss="modal"]') || firstUsable(modal, '.btn-close');
		if (modal.classList.contains('ju-modal--critical')) {
			return firstUsable(body, TEXT_FIELD + '[required]') || safe;
		}
		return firstUsable(body, TEXT_FIELD) || firstUsable(body, FOCUSABLE) || safe;
	}

	function focusSafely(el) {
		try { el.focus({ preventScroll: true }); } catch (_) { el.focus(); }
	}

	function onShow(event) {
		var modal = event.target;
		if (!modal || !modal.classList || !modal.classList.contains('modal')) return;
		if (held.has(modal)) return; // re-shown after a hand-over: keep the original opener
		var opener = event.relatedTarget || document.activeElement;
		if (opener && opener !== document.body) openers.set(modal, opener);
	}

	function onShown(event) {
		var modal = event.target;
		if (!modal || !modal.classList || !modal.classList.contains('modal')) return;
		var back = held.get(modal);
		held.delete(modal);
		var target = back && modal.contains(back) && usable(back) ? back : pickInitialFocus(modal);
		if (target) focusSafely(target);
	}

	function onHidden(event) {
		var modal = event.target;
		if (!modal || !modal.classList || !modal.classList.contains('modal')) return;
		if (skipReturn.delete(modal)) { openers.delete(modal); return; }
		if (held.has(modal)) return; // hand-over in progress: the dialog is shown again, focus stays put
		var opener = openers.get(modal);
		openers.delete(modal);
		if (document.querySelector('.modal.show')) return; // another dialog still owns focus
		if (opener && opener.isConnected && typeof opener.focus === 'function' && usable(opener)) {
			focusSafely(opener);
			return;
		}
		// Opener gone or hidden: fall back to the main landmark so keyboard users keep their place.
		var fallback = document.querySelector('main, [role="main"]');
		if (fallback) {
			if (!fallback.hasAttribute('tabindex')) fallback.setAttribute('tabindex', '-1');
			focusSafely(fallback);
		}
	}

	/** Mark `modal` as temporarily hidden for a child dialog; call before hide(). */
	function hold(modal) {
		var active = document.activeElement;
		held.set(modal, active && modal.contains(active) ? active : null);
	}
	/** The next hidden event of the child `modal` does not return focus: its parent is shown again. */
	function handBack(modal) { skipReturn.add(modal); }
	/** Cancel a hold when the modal will not be shown again. */
	function release(modal) { held.delete(modal); }
	document.addEventListener('show.bs.modal', onShow);
	document.addEventListener('shown.bs.modal', onShown);
	document.addEventListener('hidden.bs.modal', onHidden);

	root.JuModalFocus = { pickInitialFocus: pickInitialFocus, hold: hold, release: release, handBack: handBack };
})(window);
