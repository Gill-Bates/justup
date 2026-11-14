//
// app/static/js/utils/field-errors.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Inline field errors for forms and modals (DESIGN.md DC-FORM-6..10, section 6.2).
 *
 *   JuFieldErrors.show(root, [{ id: 'recipient-name', message: 'Please enter a name' }]) // true if any
 *
 * Each error gets its own element directly below the field (aria-invalid="true", linked through
 * aria-describedby, text plus icon), focus moves to the first invalid field, and the error clears when
 * the user edits the field. The error element has no role (DC-FORM-11); request-level errors stay in
 * the form's role="alert" area, not here.
 * Open/close of a modal resets every field error in it.
 */
(function (root) {
	'use strict';

	var SUFFIX = '-field-error';

	function errorId(input) { return input.id + SUFFIX; }

	function anchorOf(input) {
		if (input.closest('.ju-switch')) return input.closest('.ju-switch');
		if (input.closest('.tag-input__field')) return input.closest('.tag-input__field');
		if (input.closest('.tcp-ports-container')) return input.closest('.tcp-ports-container');
		// Keep the error below the whole row when the control sits in a Bootstrap input group.
		return input.parentElement && input.parentElement.classList.contains('input-group') ? input.parentElement : input;
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

	function renderMessage(id, message, anchor) {
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
			anchor.insertAdjacentElement('afterend', el);
		}
		el.querySelector('.ju-field-error-text').textContent = message;
	}

	/** Mark one field invalid and render its message below it. */
	function set(input, message) {
		if (!input || !input.id) return;
		var id = errorId(input);
		renderMessage(id, message, anchorOf(input));
		input.setAttribute('aria-invalid', 'true');
		input.classList.add('is-invalid');
		addDescribedBy(input, id);
	}

	var GROUP = '[data-ju-field-group]';
	function controlsOf(group) {
		return Array.prototype.slice.call(group.querySelectorAll('input, select, textarea, button')).filter(function (c) { return !c.disabled || c.getAttribute('aria-invalid') === 'true'; });
	}
	/** Mark a fieldset group invalid: one message below the group, linked from the group and its controls. */
	function setGroup(group, message) {
		if (!group || !group.id) return;
		var id = errorId(group);
		renderMessage(id, message, group);
		group.setAttribute('data-ju-invalid', 'true');
		addDescribedBy(group, id);
		group.querySelectorAll('input, select, textarea').forEach(function (c) {
			c.setAttribute('aria-invalid', 'true');
			c.classList.add('is-invalid');
			addDescribedBy(c, id);
		});
	}
	/** Remove the error of a group. */
	function clearGroup(group) {
		if (!group || !group.id) return;
		var id = errorId(group);
		var el = document.getElementById(id);
		if (el) el.remove();
		group.removeAttribute('data-ju-invalid');
		removeDescribedBy(group, id);
		group.querySelectorAll('input, select, textarea').forEach(function (c) {
			c.removeAttribute('aria-invalid');
			c.classList.remove('is-invalid');
			removeDescribedBy(c, id);
		});
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
		scope.querySelectorAll('[data-ju-invalid]').forEach(clearGroup);
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
			if (input.matches(GROUP)) {
				setGroup(input, err.message);
				var target = controlsOf(input).filter(function (c) { return !c.value; })[0] || controlsOf(input)[0];
				if (target && !first) first = target;
				return;
			}
			set(input, err.message);
			if (!first) first = input;
		});
		if (first) first.focus();
		return !!first;
	}

	function onEdit(event) {
		var input = event.target;
		var group = input && input.closest && input.closest('[data-ju-invalid]');
		// A blur-time change event of the just-reset control is no edit; only input clears a group.
		if (group) { if (event.type === 'input') clearGroup(group); return; }
		if (input && input.getAttribute && input.getAttribute('aria-invalid') === 'true' && input.id &&
			document.getElementById(errorId(input))) clear(input);
	}

	document.addEventListener('input', onEdit);
	document.addEventListener('change', onEdit);
	document.addEventListener('show.bs.modal', function (event) { clearAll(event.target); });
	document.addEventListener('hidden.bs.modal', function (event) { clearAll(event.target); });

	root.JuFieldErrors = { setGroup: setGroup, clearGroup: clearGroup, set: set, clear: clear, clearAll: clearAll, show: show };
})(window);
