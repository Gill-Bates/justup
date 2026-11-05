//
// app/static/js/utils/field-errors.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Inline field errors for forms and modals (DESIGN.md, "Validation and error display").
 *
 *   JuFieldErrors.show(root, [{ id: 'recipient-name', message: 'Please enter a name' }]) // true if any
 *
 * Each error gets its own element directly below the field (aria-invalid="true", linked through
 * aria-describedby, text plus icon), focus moves to the first invalid field, and the error clears when
 * the user edits the field. Request-level errors stay in the form's role="alert" area, not here.
 * Open/close of a modal resets every field error in it.
 */
(function (root) {
	'use strict';

	var SUFFIX = '-field-error';

	function errorId(input) { return input.id + SUFFIX; }

	function anchorOf(input) {
		return input.closest('.ju-switch') || input;
	}

	function addDescribedBy(input, id) {
		var ids = (input.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
		if (ids.indexOf(id) === -1) ids.unshift(id);
		input.setAttribute('aria-describedby', ids.join(' '));
	}

	function removeDescribedBy(input, id) {
		var ids = (input.getAttribute('aria-describedby') || '').split(/\s+/).filter(function (v) { return v && v !== id; });
		if (ids.length) input.setAttribute('aria-describedby', ids.join(' '));
		else input.removeAttribute('aria-describedby');
	}

	/** Mark one field invalid and render its message below it. */
	function set(input, message) {
		if (!input || !input.id) return;
		var id = errorId(input);
		var el = document.getElementById(id);
		if (!el) {
			el = document.createElement('div');
			el.id = id;
			el.className = 'ju-field-error';
			var icon = document.createElement('span');
			icon.className = 'material-icons';
			icon.setAttribute('aria-hidden', 'true');
			icon.textContent = 'error';
			var text = document.createElement('span');
			text.className = 'ju-field-error-text';
			el.append(icon, text);
			anchorOf(input).insertAdjacentElement('afterend', el);
		}
		el.querySelector('.ju-field-error-text').textContent = message;
		input.setAttribute('aria-invalid', 'true');
		input.classList.add('is-invalid');
		addDescribedBy(input, id);
	}

	/** Remove the error of one field. */
	function clear(input) {
		if (!input || !input.id) return;
		var id = errorId(input);
		var el = document.getElementById(id);
		if (el) el.remove();
		input.removeAttribute('aria-invalid');
		input.classList.remove('is-invalid');
		removeDescribedBy(input, id);
	}

	/** Remove all field errors below `scope` (a form or a modal). */
	function clearAll(scope) {
		if (!scope) return;
		scope.querySelectorAll('[aria-invalid="true"]').forEach(clear);
		scope.querySelectorAll('.ju-field-error').forEach(function (el) { el.remove(); });
	}

	/**
	 * Replace the field errors of `scope` by `errors` and focus the first invalid field.
	 * @param {Element} scope
	 * @param {{id: string, message: string}[]} errors
	 * @returns {boolean} true when at least one error was shown
	 */
	function show(scope, errors) {
		clearAll(scope);
		var first = null;
		(errors || []).forEach(function (err) {
			var input = document.getElementById(err.id);
			if (!input) return;
			set(input, err.message);
			if (!first) first = input;
		});
		if (first) first.focus();
		return !!first;
	}

	function onEdit(event) {
		var input = event.target;
		if (input && input.getAttribute && input.getAttribute('aria-invalid') === 'true' && input.id &&
			document.getElementById(errorId(input))) clear(input);
	}

	document.addEventListener('input', onEdit);
	document.addEventListener('change', onEdit);
	document.addEventListener('show.bs.modal', function (event) { clearAll(event.target); });
	document.addEventListener('hidden.bs.modal', function (event) { clearAll(event.target); });

	root.JuFieldErrors = { set: set, clear: clear, clearAll: clearAll, show: show };
})(window);
