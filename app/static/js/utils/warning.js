//
// app/static/js/utils/warning.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Shared S9 warning marker (DESIGN.md DC-WARN-1). Message text is always inserted as text.
(function (root) {
	'use strict';

	function create(message) {
		var warning = document.createElement('span');
		warning.className = 'ju-warning';

		var icon = document.createElement('span');
		icon.className = 'material-icons';
		icon.setAttribute('aria-hidden', 'true');
		icon.textContent = 'warning';

		var text = document.createElement('span');
		text.textContent = String(message ?? '');
		warning.append(icon, text);
		return warning;
	}

	function append(container, message) {
		var warning = create(message);
		container.appendChild(warning);
		return warning;
	}

	root.JuWarning = Object.freeze({ create: create, append: append });
})(typeof window !== 'undefined' ? window : globalThis);
